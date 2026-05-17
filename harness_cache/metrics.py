from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil


def _ratio(numerator: float, denominator: float) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


@dataclass
class MetricsCollector:
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    latencies_ms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def increment(self, name: str, amount: float = 1.0) -> None:
        self.counters[name] += amount

    def observe_latency(self, name: str, value_ms: float) -> None:
        self.latencies_ms[name].append(value_ms)

    def percentile_latency(self, name: str, percentile: float) -> float:
        values = sorted(self.latencies_ms[name])
        if not values:
            return 0.0
        percentile = min(100.0, max(0.0, percentile))
        index = max(0, ceil((percentile / 100.0) * len(values)) - 1)
        return values[index]

    def ttfe_p50(self) -> float:
        return self.percentile_latency("ttfe", 50)

    def ttfe_p95(self) -> float:
        return self.percentile_latency("ttfe", 95)

    def invalidation_latency_p95(self) -> float:
        return self.percentile_latency("invalidation", 95)

    def token_io_reduction(self) -> float:
        return 1.0 - _ratio(self.counters["tokens_with_cache"], self.counters["tokens_without_cache"])

    def full_document_read_avoidance_rate(self) -> float:
        return _ratio(self.counters["avoided_full_reads"], self.counters["total_tasks"])

    def tool_call_reduction(self) -> float:
        return 1.0 - _ratio(self.counters["tool_calls_with_cache"], self.counters["tool_calls_without_cache"])

    def stale_l1_hit_rate(self) -> float:
        return _ratio(self.counters["stale_l1_hits"], self.counters["total_l1_hits"])

    def invalid_pointer_usage_rate(self) -> float:
        return _ratio(self.counters["invalid_pointer_uses"], self.counters["total_pointer_uses"])

    def version_conflict_rate(self) -> float:
        return _ratio(self.counters["version_conflict_commits"], self.counters["attempted_commits"])

    def pointer_drift_rate(self) -> float:
        return _ratio(self.counters["drifted_pointers"], self.counters["verified_pointers"])

    def reanchor_success_rate(self) -> float:
        return _ratio(self.counters["successful_reanchors"], self.counters["attempted_reanchors"])

    def fresh_pointer_ratio(self) -> float:
        return _ratio(self.counters["fresh_pointers"], self.counters["active_pointers"])

    def useful_l1_hit_rate(self) -> float:
        return _ratio(self.counters["useful_l1_hits"], self.counters["total_l1_hits"])

    def pollution_rate(self) -> float:
        return _ratio(self.counters["polluted_promoted_pointers"], self.counters["promoted_pointers"])

    def false_promotion_rate(self) -> float:
        return _ratio(self.counters["false_promotions"], self.counters["total_promotions"])

    def eviction_regret_rate(self) -> float:
        return _ratio(self.counters["useful_evicted_pointers_reintroduced"], self.counters["total_evictions"])

    def citation_accuracy(self) -> float:
        return _ratio(self.counters["correct_citations"], self.counters["total_citations"])

    def task_success_rate(self) -> float:
        return _ratio(self.counters["successful_tasks"], self.counters["total_tasks"])

    def evidence_coverage(self) -> float:
        return _ratio(self.counters["supported_key_claims"], self.counters["total_key_claims"])

    def contradicted_pointer_rate(self) -> float:
        return _ratio(self.counters["contradicted_used_pointers"], self.counters["used_pointers"])

    def lease_contention_rate(self) -> float:
        return _ratio(self.counters["contested_lease_requests"], self.counters["total_lease_requests"])

    def duplicate_work_rate(self) -> float:
        return _ratio(self.counters["duplicate_pointer_work_units"], self.counters["total_pointer_work_units"])

    def agent_l1_divergence(self) -> float:
        return _ratio(self.counters["divergent_pointer_copies"], self.counters["shared_pointer_copies"])

    def missed_snoop_rate(self) -> float:
        return _ratio(self.counters["missed_snoop_events"], self.counters["expected_snoop_events"])

    def health_score(self) -> float:
        efficiency = self._score_positive(self.token_io_reduction(), 0.5)
        coherence = self._score_negative(self.stale_l1_hit_rate(), 0.05)
        freshness = self._score_positive(self.fresh_pointer_ratio(), 0.9)
        quality = self._score_negative(self.pollution_rate(), 0.1)
        correctness = self._score_positive(self.citation_accuracy(), 0.9)
        multi_agent = self._score_negative(self.lease_contention_rate(), 0.1)
        return (
            0.20 * efficiency
            + 0.20 * coherence
            + 0.15 * freshness
            + 0.20 * quality
            + 0.15 * correctness
            + 0.10 * multi_agent
        )

    def alerts(self) -> tuple[str, ...]:
        alerts: list[str] = []
        if self.invalid_pointer_usage_rate() > 0.001:
            alerts.append("Invalid Pointer Usage Rate > 0.1%")
        if self.stale_l1_hit_rate() > 0.05:
            alerts.append("Stale L1 Hit Rate > 5%")
        if self.missed_snoop_rate() > 0.01:
            alerts.append("Missed Snoop Rate > 1%")
        if self.pointer_drift_rate() > 0.10:
            alerts.append("Pointer Drift Rate > 10%")
        if self.pollution_rate() > 0.10:
            alerts.append("Pollution Rate > 10%")
        if self.counters["total_citations"] > 0 and self.citation_accuracy() < 0.80:
            alerts.append("Citation Accuracy < 80%")
        if self.lease_contention_rate() > 0.50:
            alerts.append("Lease Contention Rate spikes abnormally")
        if self.counters["attempted_reanchors"] > 0 and self.reanchor_success_rate() < 0.60:
            alerts.append("Re-anchor Success Rate < 60%")
        return tuple(alerts)

    @staticmethod
    def _score_positive(value: float, target: float) -> float:
        if target == 0:
            return 100.0
        return min(100.0, max(0.0, 100.0 * value / target))

    @staticmethod
    def _score_negative(value: float, warning: float) -> float:
        if warning == 0:
            return 100.0 if value == 0 else 0.0
        return min(100.0, max(0.0, 100.0 * (1.0 - value / warning)))
