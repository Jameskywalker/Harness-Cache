from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import uuid4


CORE_LOCATION_FIELDS = {
    "path",
    "line_start",
    "line_end",
    "anchors",
    "range_hash",
    "source_version",
    "pointer_version",
}

STRONG_EVIDENCE_TYPES = {
    "cited",
    "patch_linked",
    "test_passed",
    "user_accepted",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def hash_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class CoherenceState(str, Enum):
    MODIFIED = "M"
    OWNED = "O"
    EXCLUSIVE = "E"
    SHARED = "S"
    INVALID = "I"


class QualityState(str, Enum):
    CANDIDATE = "Candidate"
    PROBATION = "Probation"
    CLEAN = "Clean"
    SUSPECT = "Suspect"
    QUARANTINED = "Quarantined"
    INVALID = "Invalid"


class EvidenceType(str, Enum):
    SEEN = "seen"
    OPENED = "opened"
    USED = "used"
    CITED = "cited"
    PATCH_LINKED = "patch_linked"
    TEST_PASSED = "test_passed"
    USER_ACCEPTED = "user_accepted"
    FAILED = "failed"
    CONTRADICTED = "contradicted"


class LeaseState(str, Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    EXPIRED = "expired"
    RELEASED = "released"


class HarnessCacheError(Exception):
    pass


class PointerNotFoundError(HarnessCacheError):
    pass


class InvalidPointerError(HarnessCacheError):
    pass


class StalePointerError(HarnessCacheError):
    pass


class LeaseConflictError(HarnessCacheError):
    pass


class LeaseRejectedError(HarnessCacheError):
    pass


@dataclass
class Pointer:
    pointer_id: str
    source_id: str
    path: str
    title: str = ""
    source_type: str = "text"
    line_start: int = 1
    line_end: int = 1
    anchors: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    source_version: str = ""
    range_hash: str = ""
    pointer_version: int = 1
    coherence_state: CoherenceState = CoherenceState.SHARED
    trust_score: float = 0.5
    pollution_score: float = 0.0
    project_id: str = "default"
    quality_state: QualityState = QualityState.CANDIDATE
    usage_count: int = 0
    last_used_at: datetime | None = None
    last_verified_at: datetime | None = None
    invalidated_at: datetime | None = None
    anchor_confidence: float = 1.0
    drift_score: float = 0.0
    reanchor_attempts: int = 0
    last_reanchor_result: str | None = None
    owner_agent: str | None = None
    owner_service: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.anchors = tuple(self.anchors)
        self.tags = tuple(self.tags)
        self.coherence_state = CoherenceState(self.coherence_state)
        self.quality_state = QualityState(self.quality_state)
        self.trust_score = min(1.0, max(0.0, float(self.trust_score)))
        self.pollution_score = min(1.0, max(0.0, float(self.pollution_score)))
        self.anchor_confidence = min(1.0, max(0.0, float(self.anchor_confidence)))
        self.drift_score = min(1.0, max(0.0, float(self.drift_score)))
        if self.line_start < 1:
            raise ValueError("line_start must be one-based")
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        if self.pointer_version < 1:
            raise ValueError("pointer_version must be positive")

    @property
    def is_prompt_usable(self) -> bool:
        return self.coherence_state in {CoherenceState.SHARED, CoherenceState.EXCLUSIVE}

    def copy(self, **changes: Any) -> Pointer:
        return replace(self, **changes)


@dataclass(frozen=True)
class SourceRange:
    source_id: str
    path: str
    source_version: str
    line_start: int
    line_end: int
    text: str
    range_hash: str
    anchors_found: tuple[str, ...]
    fresh: bool


@dataclass(frozen=True)
class FreshnessResult:
    fresh: bool
    source_exists: bool
    source_version_matches: bool
    range_hash_matches: bool
    anchor_confidence: float
    drift_score: float
    reason: str


@dataclass
class Lease:
    lease_id: str
    pointer_id: str
    holder_agent: str
    purpose: str
    lease_state: LeaseState
    granted_at: datetime
    expires_at: datetime

    @classmethod
    def grant(
        cls,
        pointer_id: str,
        holder_agent: str,
        purpose: str,
        ttl_ms: int,
        now: datetime | None = None,
    ) -> Lease:
        now = now or utcnow()
        return cls(
            lease_id=new_id("lease"),
            pointer_id=pointer_id,
            holder_agent=holder_agent,
            purpose=purpose,
            lease_state=LeaseState.ACTIVE,
            granted_at=now,
            expires_at=now + timedelta(milliseconds=ttl_ms),
        )

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utcnow()
        return self.lease_state == LeaseState.ACTIVE and now < self.expires_at


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    agent_id: str
    pointer_id: str
    task_id: str
    evidence_type: EvidenceType
    weight: float
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)

    @classmethod
    def create(
        cls,
        agent_id: str,
        pointer_id: str,
        task_id: str,
        evidence_type: EvidenceType | str,
        weight: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        evidence_type = EvidenceType(evidence_type)
        default_weights = {
            EvidenceType.SEEN: 0.0,
            EvidenceType.OPENED: 0.05,
            EvidenceType.USED: 0.2,
            EvidenceType.CITED: 0.35,
            EvidenceType.PATCH_LINKED: 0.4,
            EvidenceType.TEST_PASSED: 0.4,
            EvidenceType.USER_ACCEPTED: 0.45,
            EvidenceType.FAILED: -0.35,
            EvidenceType.CONTRADICTED: -0.6,
        }
        return cls(
            evidence_id=new_id("ev"),
            agent_id=agent_id,
            pointer_id=pointer_id,
            task_id=task_id,
            evidence_type=evidence_type,
            weight=default_weights[evidence_type] if weight is None else weight,
            metadata=metadata or {},
        )

    @property
    def is_strong(self) -> bool:
        return self.evidence_type.value in STRONG_EVIDENCE_TYPES


@dataclass(frozen=True)
class Event:
    seq: int
    event_type: str
    created_at: datetime
    data: dict[str, Any]


@dataclass(frozen=True)
class CommitResult:
    success: bool
    pointer: Pointer | None
    events: tuple[Event, ...]
    reason: str


@dataclass(frozen=True)
class PromotionDecision:
    pointer_id: str
    promoted: bool
    from_state: QualityState
    to_state: QualityState
    reason: str


@dataclass(frozen=True)
class HarnessCacheConfig:
    l1_max_entries_per_agent: int = 50
    l1_ttl_minutes: int = 60
    require_read_time_version_check: bool = True
    l2_max_entries_per_project: int = 5000
    min_trust_score: float = 0.45
    probation_enabled: bool = True
    lease_ttl_seconds: int = 300
    require_source_version: bool = True
    require_range_hash: bool = True
    reanchor_on_source_change: bool = True
    min_anchor_confidence: float = 0.75
    require_strong_evidence_for_hot_set: bool = True
    min_trust_for_l2: float = 0.45
    min_trust_for_hot_set: float = 0.70
    quorum_for_hot_set: int = 2
    demote_on_repeated_open_unused: bool = True
    demote_on_failed_task: bool = True
    quarantine_on_contradiction: bool = True
