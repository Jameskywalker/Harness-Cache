from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from .models import (
    EvidenceType,
    InvalidPointerError,
    Pointer,
    SourceRange,
    StalePointerError,
    new_id,
    utcnow,
)

if TYPE_CHECKING:
    from .cache import HarnessCache


TokenEstimator = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class EvidenceRange:
    pointer: Pointer
    source_range: SourceRange
    estimated_tokens: int


@dataclass(frozen=True)
class TaskReport:
    task_id: str
    agent_id: str
    task: str
    opened_ranges: int
    skipped_pointers: int
    tokens_with_cache: int
    tokens_without_cache: int
    token_io_reduction: float
    tool_calls_with_cache: int
    tool_calls_without_cache: int
    success: bool | None


@dataclass(frozen=True)
class WorkflowComparison:
    tasks: int
    tokens_with_cache: int
    tokens_without_cache: int
    saved_tokens: int
    token_io_reduction: float
    tool_calls_with_cache: int
    tool_calls_without_cache: int

    @property
    def saves_tokens(self) -> bool:
        return self.saved_tokens > 0


def compare_reports(reports: list[TaskReport] | tuple[TaskReport, ...]) -> WorkflowComparison:
    tokens_with_cache = sum(report.tokens_with_cache for report in reports)
    tokens_without_cache = sum(report.tokens_without_cache for report in reports)
    tool_calls_with_cache = sum(report.tool_calls_with_cache for report in reports)
    tool_calls_without_cache = sum(report.tool_calls_without_cache for report in reports)
    saved_tokens = tokens_without_cache - tokens_with_cache
    reduction = 0.0 if tokens_without_cache == 0 else 1.0 - tokens_with_cache / tokens_without_cache
    return WorkflowComparison(
        tasks=len(reports),
        tokens_with_cache=tokens_with_cache,
        tokens_without_cache=tokens_without_cache,
        saved_tokens=saved_tokens,
        token_io_reduction=reduction,
        tool_calls_with_cache=tool_calls_with_cache,
        tool_calls_without_cache=tool_calls_without_cache,
    )


@dataclass
class AgentTaskSession:
    cache: HarnessCache
    agent_id: str
    task: str
    project_id: str = "default"
    task_id: str = field(default_factory=lambda: new_id("task"))
    token_estimator: TokenEstimator = estimate_tokens
    started_at: datetime = field(default_factory=utcnow)
    pointers: list[Pointer] = field(default_factory=list)
    ranges: list[EvidenceRange] = field(default_factory=list)
    skipped_pointer_ids: list[str] = field(default_factory=list)
    touched_pointer_ids: set[str] = field(default_factory=set)
    success: bool | None = None
    _first_evidence_recorded: bool = False
    _baseline_sources: set[tuple[str, str]] = field(default_factory=set)
    _avoided_full_read_sources: set[tuple[str, str]] = field(default_factory=set)
    _tokens_with_cache: int = 0
    _tokens_without_cache: int = 0
    _tool_calls_with_cache: int = 0
    _tool_calls_without_cache: int = 0

    def find_evidence(
        self,
        limit: int = 5,
        required_tags: list[str] | tuple[str, ...] | None = None,
        open_limit: int | None = 1,
        verify_freshness: bool = True,
    ) -> list[EvidenceRange]:
        self._record_cache_tool_call()
        self.pointers = self.cache.search(
            {
                "agent_id": self.agent_id,
                "task": self.task,
                "project_id": self.project_id,
                "limit": limit,
                "required_tags": list(required_tags or ()),
                "task_id": self.task_id,
            }
        )
        max_opens = limit if open_limit is None else min(open_limit, limit)
        for pointer in self.pointers[:max_opens]:
            self._record_cache_tool_call()
            try:
                source_range = self.cache.open(
                    pointer.pointer_id,
                    agent_id=self.agent_id,
                    task_id=self.task_id,
                    verify_freshness=verify_freshness,
                )
            except (InvalidPointerError, StalePointerError):
                self.skipped_pointer_ids.append(pointer.pointer_id)
                self.cache.record_evidence(
                    {
                        "agent_id": self.agent_id,
                        "pointer_id": pointer.pointer_id,
                        "task_id": self.task_id,
                        "evidence_type": EvidenceType.FAILED,
                    }
                )
                continue
            self._record_baseline_for(pointer)
            tokens = self.token_estimator(source_range.text)
            self._tokens_with_cache += tokens
            self.cache.metrics.increment("tokens_with_cache", tokens)
            self.touched_pointer_ids.add(pointer.pointer_id)
            self.ranges.append(EvidenceRange(pointer, source_range, tokens))
            self._record_ttfe_once()
        return list(self.ranges)

    def use(self, pointer_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.USED, metadata)

    def cite(self, pointer_id: str, correct: bool = True, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.CITED, metadata)
        if correct:
            self.cache.metrics.increment("correct_citations")
        else:
            self.cache.metrics.increment("contradicted_used_pointers")
            self._record_evidence(pointer_id, EvidenceType.CONTRADICTED, metadata)

    def patch_linked(self, pointer_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.PATCH_LINKED, metadata)

    def test_passed(self, pointer_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.TEST_PASSED, metadata)

    def user_accepted(self, pointer_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.USER_ACCEPTED, metadata)

    def failed(self, pointer_id: str, metadata: dict[str, Any] | None = None) -> None:
        self._record_evidence(pointer_id, EvidenceType.FAILED, metadata)

    def complete(
        self,
        success: bool,
        supported_key_claims: int = 0,
        total_key_claims: int = 0,
        promote_touched: bool = True,
    ) -> TaskReport:
        self.success = success
        if success:
            self.cache.metrics.increment("successful_tasks")
        self.cache.metrics.increment("supported_key_claims", supported_key_claims)
        self.cache.metrics.increment("total_key_claims", total_key_claims)
        if promote_touched:
            for pointer_id in sorted(self.touched_pointer_ids):
                self.cache.evaluate_promotion(pointer_id)
        return self.report()

    def report(self) -> TaskReport:
        return TaskReport(
            task_id=self.task_id,
            agent_id=self.agent_id,
            task=self.task,
            opened_ranges=len(self.ranges),
            skipped_pointers=len(self.skipped_pointer_ids),
            tokens_with_cache=self._tokens_with_cache,
            tokens_without_cache=self._tokens_without_cache,
            token_io_reduction=(
                0.0
                if self._tokens_without_cache == 0
                else 1.0 - self._tokens_with_cache / self._tokens_without_cache
            ),
            tool_calls_with_cache=self._tool_calls_with_cache,
            tool_calls_without_cache=self._tool_calls_without_cache,
            success=self.success,
        )

    def read_full_source(self, source_id: str, path: str) -> str:
        document = self.cache.sources.get(source_id, path)
        if document is None:
            raise FileNotFoundError(f"unknown source {source_id}:{path}")
        tokens = self.token_estimator(document.text)
        self._tokens_with_cache += tokens
        self.cache.metrics.increment("tokens_with_cache", tokens)
        self._record_cache_tool_call()
        self._record_no_cache_baseline(source_id, path, avoided_full_read=False)
        return document.text

    def record_no_cache_baseline(
        self,
        source_id: str,
        path: str,
        avoided_full_read: bool = False,
    ) -> int:
        return self._record_no_cache_baseline(source_id, path, avoided_full_read)

    def _record_evidence(
        self,
        pointer_id: str,
        evidence_type: EvidenceType,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.touched_pointer_ids.add(pointer_id)
        self.cache.record_evidence(
            {
                "agent_id": self.agent_id,
                "pointer_id": pointer_id,
                "task_id": self.task_id,
                "evidence_type": evidence_type,
                "metadata": metadata or {},
            }
        )

    def _record_baseline_for(self, pointer: Pointer) -> None:
        self._record_no_cache_baseline(pointer.source_id, pointer.path, avoided_full_read=True)

    def _record_no_cache_baseline(
        self,
        source_id: str,
        path: str,
        avoided_full_read: bool,
    ) -> int:
        source_key = (source_id, path)
        tokens = 0
        if source_key in self._baseline_sources:
            tokens = 0
        else:
            document = self.cache.sources.get(source_id, path)
            if document is None:
                raise FileNotFoundError(f"unknown source {source_id}:{path}")
            self._baseline_sources.add(source_key)
            tokens = self.token_estimator(document.text)
            self._tokens_without_cache += tokens
            self.cache.metrics.increment("tokens_without_cache", tokens)
            self._tool_calls_without_cache += 1
            self.cache.metrics.increment("tool_calls_without_cache")
        if avoided_full_read and source_key not in self._avoided_full_read_sources:
            self.cache.metrics.increment("avoided_full_reads")
            self._avoided_full_read_sources.add(source_key)
        return tokens

    def _record_cache_tool_call(self) -> None:
        self._tool_calls_with_cache += 1
        self.cache.metrics.increment("tool_calls_with_cache")

    def _record_ttfe_once(self) -> None:
        if self._first_evidence_recorded:
            return
        elapsed_ms = (utcnow() - self.started_at).total_seconds() * 1000
        self.cache.metrics.observe_latency("ttfe", elapsed_ms)
        self._first_evidence_recorded = True


class CachedAgentWorkflow:
    def __init__(self, cache: HarnessCache, token_estimator: TokenEstimator = estimate_tokens) -> None:
        self.cache = cache
        self.token_estimator = token_estimator

    def start_task(
        self,
        agent_id: str,
        task: str,
        project_id: str = "default",
        task_id: str | None = None,
    ) -> AgentTaskSession:
        self.cache.metrics.increment("total_tasks")
        return AgentTaskSession(
            cache=self.cache,
            agent_id=agent_id,
            task=task,
            project_id=project_id,
            task_id=task_id or new_id("task"),
            token_estimator=self.token_estimator,
        )
