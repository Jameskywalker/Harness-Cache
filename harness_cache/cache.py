from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any, Iterable

from .coherence import CoherenceManager
from .events import EventLog
from .metrics import MetricsCollector
from .models import (
    CoherenceState,
    CommitResult,
    Evidence,
    EvidenceType,
    Event,
    FreshnessResult,
    HarnessCacheConfig,
    InvalidPointerError,
    Lease,
    Pointer,
    PointerNotFoundError,
    PromotionDecision,
    QualityState,
    SourceRange,
    StalePointerError,
    STRONG_EVIDENCE_TYPES,
    utcnow,
)
from .source import SourceRepository


class HarnessCache:
    def __init__(self, config: HarnessCacheConfig | None = None) -> None:
        self.config = config or HarnessCacheConfig()
        self.sources = SourceRepository()
        self.event_log = EventLog()
        self.metrics = MetricsCollector()
        self.coherence = CoherenceManager(self.event_log, self.config)
        self.l1: dict[str, OrderedDict[str, Pointer]] = defaultdict(OrderedDict)
        self.l2: dict[str, Pointer] = {}
        self.l3: dict[str, Pointer] = {}
        self.candidate_pool: dict[str, Pointer] = {}
        self.probation: dict[str, Pointer] = {}
        self.hot_set: set[str] = set()
        self.evidence: dict[str, list[Evidence]] = defaultdict(list)
        self.agent_last_processed_seq: dict[str, int] = defaultdict(int)

    def add_source(self, source_id: str, path: str, text: str, version: str) -> None:
        self.sources.add(source_id, path, text, version)

    def add_pointer(self, pointer: Pointer, level: str = "candidate") -> Pointer:
        pointer = self.sources.hydrate_pointer_hash(pointer)
        pointer = pointer.copy(coherence_state=CoherenceState.SHARED)
        level = level.lower()
        self.l3[pointer.pointer_id] = pointer
        if level == "candidate":
            pointer = pointer.copy(quality_state=QualityState.CANDIDATE)
            self.candidate_pool[pointer.pointer_id] = pointer
        elif level == "probation":
            pointer = pointer.copy(quality_state=QualityState.PROBATION)
            self.probation[pointer.pointer_id] = pointer
        elif level == "l2":
            pointer = pointer.copy(quality_state=QualityState.CLEAN)
            self.l2[pointer.pointer_id] = pointer
        elif level == "hot":
            pointer = pointer.copy(quality_state=QualityState.CLEAN)
            self.l2[pointer.pointer_id] = pointer
            self.hot_set.add(pointer.pointer_id)
        else:
            raise ValueError(f"unknown pointer level: {level}")
        self.l3[pointer.pointer_id] = pointer
        self.event_log.append("POINTER_ADDED", pointer_id=pointer.pointer_id, level=level)
        return pointer

    def search(self, request: dict[str, Any]) -> list[Pointer]:
        agent_id = request["agent_id"]
        task = request.get("task", "")
        project_id = request.get("project_id", "default")
        limit = int(request.get("limit", self.config.l1_max_entries_per_agent))
        required_tags = set(request.get("required_tags") or [])
        exclude_invalid = request.get("exclude_invalid", True)

        self.replay_events(agent_id)

        candidates = self._unique_pointers(
            list(self.l1[agent_id].values())
            + list(self.l2.values())
            + [self.l3[pointer_id] for pointer_id in self.hot_set if pointer_id in self.l3]
            + list(self.probation.values())
            + list(self.candidate_pool.values())
            + list(self.l3.values())
        )
        scored: list[tuple[float, Pointer]] = []
        for pointer in candidates:
            if pointer.project_id != project_id:
                continue
            if required_tags and not required_tags.issubset(pointer.tags):
                continue
            if exclude_invalid and pointer.coherence_state == CoherenceState.INVALID:
                continue
            score = self._score_pointer(pointer, task)
            if score > 0:
                scored.append((score, pointer))

        scored.sort(key=lambda item: item[0], reverse=True)
        results = [pointer for _, pointer in scored[:limit]]
        for pointer in results:
            self._put_l1(agent_id, pointer)
            self.coherence.register_holder(agent_id, pointer)
            self.record_evidence(
                {
                    "agent_id": agent_id,
                    "pointer_id": pointer.pointer_id,
                    "task_id": request.get("task_id", "search"),
                    "evidence_type": EvidenceType.SEEN,
                }
            )
        return results

    def open(
        self,
        pointer_id: str,
        agent_id: str = "default",
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SourceRange:
        options = {**(options or {}), **kwargs}
        verify_freshness = options.get("verify_freshness", self.config.require_read_time_version_check)
        pointer = self._get_pointer(pointer_id, agent_id=agent_id)
        self.replay_events(agent_id)
        pointer = self._get_pointer(pointer_id, agent_id=agent_id)
        if pointer.coherence_state == CoherenceState.INVALID:
            self.metrics.increment("invalid_pointer_uses")
            self.metrics.increment("total_pointer_uses")
            raise InvalidPointerError(f"pointer {pointer_id} is invalid")
        if not pointer.is_prompt_usable:
            raise InvalidPointerError(f"pointer {pointer_id} is not directly usable in state {pointer.coherence_state.value}")

        if verify_freshness:
            freshness = self.verify_freshness(pointer_id, agent_id=agent_id)
            if not freshness.fresh:
                self._mark_pointer_state(pointer_id, CoherenceState.INVALID, invalidated_at=utcnow())
                self.metrics.increment("stale_l1_hits")
                raise StalePointerError(f"pointer {pointer_id} failed freshness check: {freshness.reason}")

        source_range = self.sources.open_range(pointer)
        self._put_l1(agent_id, pointer.copy(last_verified_at=utcnow()))
        self.coherence.register_holder(agent_id, pointer)
        self.metrics.increment("total_pointer_uses")
        self.record_evidence(
            {
                "agent_id": agent_id,
                "pointer_id": pointer_id,
                "task_id": options.get("task_id", "open"),
                "evidence_type": EvidenceType.OPENED,
            }
        )
        return source_range

    def acquire_lease(self, request: dict[str, Any]) -> Lease:
        agent_id = request["agent_id"]
        pointer_id = request["pointer_id"]
        purpose = request.get("purpose", "edit")
        ttl_ms = request.get("ttl_ms")
        pointer = self._get_pointer(pointer_id, agent_id=agent_id)
        self.metrics.increment("total_lease_requests")
        try:
            lease = self.coherence.acquire_lease(agent_id, pointer, purpose, ttl_ms)
        except Exception:
            self.metrics.increment("contested_lease_requests")
            raise
        self._store_pointer(pointer.copy(coherence_state=CoherenceState.EXCLUSIVE))
        return lease

    def commit_pointer_update(self, request: dict[str, Any]) -> CommitResult:
        agent_id = request["agent_id"]
        lease_id = request["lease_id"]
        pointer_id = request["pointer_id"]
        base_pointer_version = int(request["base_pointer_version"])
        proposed_pointer: Pointer = request["new_pointer"]
        evidence_items = request.get("evidence", ())
        events: list[Event] = []
        self.metrics.increment("attempted_commits")

        try:
            self.coherence.require_active_lease(lease_id, agent_id, pointer_id)
        except Exception as exc:
            self.metrics.increment("version_conflict_commits")
            return CommitResult(False, None, tuple(events), str(exc))

        current = self._get_pointer(pointer_id)
        if current.pointer_version != base_pointer_version:
            self.metrics.increment("version_conflict_commits")
            return CommitResult(False, None, tuple(events), "base pointer version conflict")

        proposal = proposed_pointer.copy(
            pointer_id=pointer_id,
            pointer_version=base_pointer_version,
            coherence_state=CoherenceState.MODIFIED,
        )
        proposal = self.sources.hydrate_pointer_hash(proposal)
        events.append(
            self.event_log.append(
                "POINTER_UPDATE_PROPOSED",
                pointer_id=pointer_id,
                base_pointer_version=base_pointer_version,
                lease_id=lease_id,
                agent_id=agent_id,
            )
        )

        freshness = self.sources.verify(proposal)
        if not freshness.source_exists or not freshness.source_version_matches or not freshness.range_hash_matches:
            invalid = proposal.copy(coherence_state=CoherenceState.INVALID, quality_state=QualityState.INVALID)
            self._store_pointer(invalid)
            events.append(
                self.event_log.append(
                    "POINTER_UPDATE_REJECTED",
                    pointer_id=pointer_id,
                    lease_id=lease_id,
                    agent_id=agent_id,
                    reason=freshness.reason,
                )
            )
            return CommitResult(False, invalid, tuple(events), freshness.reason)

        committed = proposal.copy(
            pointer_version=base_pointer_version + 1,
            coherence_state=CoherenceState.SHARED,
            last_verified_at=utcnow(),
            anchor_confidence=freshness.anchor_confidence,
            drift_score=freshness.drift_score,
        )
        self._store_pointer(committed)
        self.coherence.commit_lease(lease_id)
        events.append(
            self.event_log.append(
                "POINTER_COMMITTED",
                pointer_id=pointer_id,
                old_pointer_version=base_pointer_version,
                new_pointer_version=committed.pointer_version,
                lease_id=lease_id,
                agent_id=agent_id,
            )
        )
        target_agents = self.coherence.invalidate_holders(
            committed,
            old_pointer_version=base_pointer_version,
            new_pointer_version=committed.pointer_version,
            reason=f"committed_by_{agent_id}",
            exclude_agent=agent_id,
        )
        invalidation = self.coherence.append_invalidation(
            committed,
            old_pointer_version=base_pointer_version,
            reason=f"committed_by_{agent_id}",
            target_agents=target_agents,
        )
        events.append(invalidation)
        self._apply_invalidation(invalidation)
        self._put_l1(agent_id, committed)
        self.coherence.register_holder(agent_id, committed)

        for evidence in evidence_items:
            if isinstance(evidence, Evidence):
                self._record_evidence_object(evidence)
            else:
                self.record_evidence(evidence)
        return CommitResult(True, committed, tuple(events), "committed")

    def record_evidence(self, request: dict[str, Any]) -> None:
        evidence = Evidence.create(
            agent_id=request["agent_id"],
            pointer_id=request["pointer_id"],
            task_id=request.get("task_id", "default"),
            evidence_type=request["evidence_type"],
            weight=request.get("weight"),
            metadata=request.get("metadata"),
        )
        self._record_evidence_object(evidence)

    def verify_freshness(self, pointer_id: str, agent_id: str | None = None) -> FreshnessResult:
        pointer = self._get_pointer(pointer_id, agent_id=agent_id)
        result = self.sources.verify(pointer)
        self.metrics.increment("verified_pointers")
        self.metrics.increment("active_pointers")
        if result.fresh:
            self.metrics.increment("fresh_pointers")
        if result.drift_score > 0:
            self.metrics.increment("drifted_pointers")
        updated = pointer.copy(
            last_verified_at=utcnow(),
            anchor_confidence=result.anchor_confidence,
            drift_score=result.drift_score,
        )
        self._store_pointer(updated)
        if agent_id:
            self._put_l1(agent_id, updated)
        return result

    def handle_source_change(
        self,
        source_id: str,
        path: str,
        new_text: str,
        new_source_version: str,
        old_source_version: str | None = None,
        affected_ranges: list[tuple[int, int]] | None = None,
    ) -> Event:
        old_document = self.sources.get(source_id, path)
        old_version = old_source_version or (old_document.version if old_document is not None else "")
        self.sources.add(source_id, path, new_text, new_source_version)
        event = self.event_log.append(
            "SOURCE_CHANGED",
            source_id=source_id,
            path=path,
            old_source_version=old_version,
            new_source_version=new_source_version,
            affected_ranges=affected_ranges or [],
        )
        for pointer in list(self.l3.values()):
            if pointer.source_id != source_id or pointer.path != path:
                continue
            if old_version and pointer.source_version != old_version:
                continue
            next_pointer = self.sources.reanchor(pointer, self.config.min_anchor_confidence)
            self.metrics.increment("attempted_reanchors")
            if next_pointer is not None and next_pointer.last_reanchor_result == "success":
                self.metrics.increment("successful_reanchors")
                next_pointer = next_pointer.copy(
                    coherence_state=CoherenceState.OWNED,
                    owner_service="source_watcher",
                )
            else:
                next_pointer = pointer.copy(
                    source_version=new_source_version,
                    coherence_state=CoherenceState.INVALID,
                    quality_state=QualityState.INVALID,
                    invalidated_at=utcnow(),
                    anchor_confidence=0.0,
                    drift_score=1.0,
                    last_reanchor_result="failed",
                )
            self._store_pointer(next_pointer)
            targets = self.coherence.invalidate_holders(
                next_pointer,
                old_pointer_version=pointer.pointer_version,
                new_pointer_version=next_pointer.pointer_version,
                reason="source_changed",
            )
            invalidation = self.coherence.append_invalidation(
                next_pointer,
                old_pointer_version=pointer.pointer_version,
                reason="source_changed",
                target_agents=targets,
            )
            self._apply_invalidation(invalidation)
        return event

    def replay_events(self, agent_id: str) -> None:
        last_seq = self.agent_last_processed_seq[agent_id]
        for event in self.event_log.since(last_seq):
            if event.event_type == "POINTER_INVALIDATE":
                self._apply_invalidation_for_agent(agent_id, event)
            self.agent_last_processed_seq[agent_id] = event.seq

    def evaluate_promotion(self, pointer_id: str) -> PromotionDecision:
        pointer = self._get_pointer(pointer_id)
        from_state = pointer.quality_state
        freshness = self.sources.verify(pointer)
        evidence = self.evidence[pointer_id]
        has_open_or_used = any(item.evidence_type in {EvidenceType.OPENED, EvidenceType.USED} for item in evidence)
        has_strong = any(item.is_strong for item in evidence)
        independent_agents = {item.agent_id for item in evidence if item.evidence_type.value in STRONG_EVIDENCE_TYPES or item.evidence_type == EvidenceType.USED}
        independent_tasks = {item.task_id for item in evidence if item.evidence_type.value in STRONG_EVIDENCE_TYPES or item.evidence_type == EvidenceType.USED}

        if pointer.quality_state == QualityState.CANDIDATE:
            if self.config.probation_enabled and freshness.fresh and has_open_or_used:
                promoted = pointer.copy(quality_state=QualityState.PROBATION)
                self.probation[pointer_id] = promoted
                self.candidate_pool.pop(pointer_id, None)
                self._store_pointer(promoted)
                return PromotionDecision(pointer_id, True, from_state, QualityState.PROBATION, "fresh pointer with usage evidence")
            return PromotionDecision(pointer_id, False, from_state, from_state, "candidate lacks freshness or usage evidence")

        if pointer_id not in self.l2:
            if freshness.fresh and pointer.trust_score >= self.config.min_trust_for_l2 and pointer.pollution_score <= 0.35:
                promoted = pointer.copy(quality_state=QualityState.CLEAN)
                self.l2[pointer_id] = promoted
                self.probation.pop(pointer_id, None)
                self.candidate_pool.pop(pointer_id, None)
                self._store_pointer(promoted)
                self.metrics.increment("total_promotions")
                self.metrics.increment("promoted_pointers")
                return PromotionDecision(pointer_id, True, from_state, QualityState.CLEAN, "passed L3 to L2 gate")
            return PromotionDecision(pointer_id, False, from_state, from_state, "failed L3 to L2 gate")

        quorum = max(len(independent_agents), len(independent_tasks))
        if pointer_id not in self.hot_set:
            if (
                pointer.trust_score >= self.config.min_trust_for_hot_set
                and pointer.pollution_score <= 0.05
                and (has_strong or not self.config.require_strong_evidence_for_hot_set)
                and quorum >= self.config.quorum_for_hot_set
            ):
                self.hot_set.add(pointer_id)
                return PromotionDecision(pointer_id, True, from_state, QualityState.CLEAN, "passed hot-set trust and quorum gate")
            return PromotionDecision(pointer_id, False, from_state, from_state, "failed hot-set trust, evidence, or quorum gate")

        return PromotionDecision(pointer_id, False, from_state, from_state, "already in hot set")

    def event_replay_lag(self, agent_id: str) -> int:
        return self.event_log.latest_seq - self.agent_last_processed_seq[agent_id]

    def agent_l1_divergence(self) -> float:
        shared = 0
        divergent = 0
        versions_by_pointer: dict[str, set[int]] = defaultdict(set)
        for l1 in self.l1.values():
            for pointer in l1.values():
                versions_by_pointer[pointer.pointer_id].add(pointer.pointer_version)
        for versions in versions_by_pointer.values():
            if len(versions) > 1:
                divergent += len(versions)
            shared += len(versions)
        self.metrics.counters["divergent_pointer_copies"] = divergent
        self.metrics.counters["shared_pointer_copies"] = shared
        return self.metrics.agent_l1_divergence()

    def _record_evidence_object(self, evidence: Evidence) -> None:
        pointer = self._get_pointer(evidence.pointer_id)
        self.evidence[evidence.pointer_id].append(evidence)
        self.event_log.append(
            "EVIDENCE_RECORDED",
            pointer_id=evidence.pointer_id,
            agent_id=evidence.agent_id,
            task_id=evidence.task_id,
            evidence_type=evidence.evidence_type.value,
            weight=evidence.weight,
        )
        trust_delta = max(0.0, evidence.weight) * 0.15
        pollution_delta = 0.0
        quality_state = pointer.quality_state
        if evidence.evidence_type == EvidenceType.FAILED:
            trust_delta = -0.08
            pollution_delta = 0.15
            quality_state = QualityState.SUSPECT
        elif evidence.evidence_type == EvidenceType.CONTRADICTED:
            trust_delta = -0.2
            pollution_delta = 0.35
            quality_state = QualityState.QUARANTINED
        elif evidence.evidence_type in {EvidenceType.USED, EvidenceType.CITED, EvidenceType.PATCH_LINKED}:
            self.metrics.increment("useful_l1_hits")
        if evidence.evidence_type == EvidenceType.CITED:
            self.metrics.increment("total_citations")
        updated = pointer.copy(
            trust_score=pointer.trust_score + trust_delta,
            pollution_score=pointer.pollution_score + pollution_delta,
            quality_state=quality_state,
            usage_count=pointer.usage_count + (1 if evidence.evidence_type != EvidenceType.SEEN else 0),
            last_used_at=utcnow() if evidence.evidence_type != EvidenceType.SEEN else pointer.last_used_at,
        )
        self._store_pointer(updated)

    def _score_pointer(self, pointer: Pointer, task: str) -> float:
        text = f"{pointer.title} {' '.join(pointer.tags)} {' '.join(pointer.anchors)} {pointer.path}".lower()
        task_terms = {term for term in task.lower().replace("/", " ").replace("_", " ").split() if term}
        overlap = sum(1 for term in task_terms if term in text)
        hot_bonus = 0.2 if pointer.pointer_id in self.hot_set else 0.0
        l2_bonus = 0.1 if pointer.pointer_id in self.l2 else 0.0
        state_bonus = 0.1 if pointer.coherence_state == CoherenceState.SHARED else 0.0
        base = 0.05 if not task_terms else 0.0
        return base + overlap + pointer.trust_score - pointer.pollution_score + hot_bonus + l2_bonus + state_bonus

    def _unique_pointers(self, pointers: Iterable[Pointer]) -> list[Pointer]:
        unique: dict[str, Pointer] = {}
        for pointer in pointers:
            existing = unique.get(pointer.pointer_id)
            if existing is None or pointer.pointer_version >= existing.pointer_version:
                unique[pointer.pointer_id] = pointer
        return list(unique.values())

    def _get_pointer(self, pointer_id: str, agent_id: str | None = None) -> Pointer:
        if agent_id is not None and pointer_id in self.l1[agent_id]:
            pointer = self.l1[agent_id][pointer_id]
            self.metrics.increment("total_l1_hits")
            return pointer
        for store in (self.l2, self.l3, self.probation, self.candidate_pool):
            if pointer_id in store:
                return store[pointer_id]
        raise PointerNotFoundError(pointer_id)

    def _store_pointer(self, pointer: Pointer) -> None:
        self.l3[pointer.pointer_id] = pointer
        if pointer.pointer_id in self.l2:
            self.l2[pointer.pointer_id] = pointer
        if pointer.pointer_id in self.probation:
            self.probation[pointer.pointer_id] = pointer
        if pointer.pointer_id in self.candidate_pool:
            self.candidate_pool[pointer.pointer_id] = pointer
        for agent_id, l1 in self.l1.items():
            if pointer.pointer_id in l1 and l1[pointer.pointer_id].pointer_version < pointer.pointer_version:
                continue
            if pointer.pointer_id in l1 and l1[pointer.pointer_id].coherence_state == CoherenceState.INVALID:
                continue
            if pointer.pointer_id in l1 and l1[pointer.pointer_id].pointer_version == pointer.pointer_version:
                l1[pointer.pointer_id] = pointer

    def _put_l1(self, agent_id: str, pointer: Pointer) -> None:
        l1 = self.l1[agent_id]
        if pointer.pointer_id in l1:
            l1.pop(pointer.pointer_id)
        l1[pointer.pointer_id] = pointer
        while len(l1) > self.config.l1_max_entries_per_agent:
            l1.popitem(last=False)
            self.metrics.increment("total_evictions")

    def _mark_pointer_state(
        self,
        pointer_id: str,
        state: CoherenceState,
        invalidated_at: datetime | None = None,
    ) -> None:
        pointer = self._get_pointer(pointer_id)
        quality = QualityState.INVALID if state == CoherenceState.INVALID else pointer.quality_state
        updated = pointer.copy(coherence_state=state, quality_state=quality, invalidated_at=invalidated_at)
        self._store_pointer(updated)

    def _apply_invalidation(self, event: Event) -> None:
        for agent_id in list(self.l1):
            self._apply_invalidation_for_agent(agent_id, event)

    def _apply_invalidation_for_agent(self, agent_id: str, event: Event) -> None:
        if event.event_type != "POINTER_INVALIDATE":
            return
        target_agents = tuple(event.data.get("target_agents") or ())
        if target_agents and agent_id not in target_agents:
            return
        pointer_id = str(event.data["pointer_id"])
        old_version = int(event.data["old_pointer_version"])
        pointer = self.l1[agent_id].get(pointer_id)
        if pointer is None:
            return
        if pointer.pointer_version <= old_version:
            self.l1[agent_id][pointer_id] = pointer.copy(
                coherence_state=CoherenceState.INVALID,
                quality_state=QualityState.INVALID,
                invalidated_at=event.created_at,
            )
            latency_ms = (utcnow() - event.created_at).total_seconds() * 1000
            self.metrics.observe_latency("invalidation", latency_ms)
