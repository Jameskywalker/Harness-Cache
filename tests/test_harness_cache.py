from __future__ import annotations

import unittest
from datetime import timedelta

from harness_cache import CachedAgentWorkflow, CoherenceState, HarnessCache, Pointer, QualityState, compare_reports
from examples.demo_token_savings import run_demo
from harness_cache.models import InvalidPointerError, StalePointerError, utcnow


SOURCE_TEXT = "\n".join(
    [
        "from tokens import decode",
        "",
        "def validate_jwt(refresh_token):",
        "    payload = decode(refresh_token)",
        "    return payload['sub']",
        "",
        "def logout():",
        "    return True",
    ]
)


def make_cache(level: str = "l2", trust_score: float = 0.6) -> tuple[HarnessCache, Pointer]:
    cache = HarnessCache()
    cache.add_source("repo_backend", "src/auth/middleware.py", SOURCE_TEXT, "commit_a")
    pointer = Pointer(
        pointer_id="ptr_auth_001",
        source_id="repo_backend",
        source_type="code",
        path="src/auth/middleware.py",
        title="JWT refresh-token validation",
        line_start=3,
        line_end=5,
        anchors=("validate_jwt", "refresh_token"),
        tags=("auth", "jwt", "middleware"),
        source_version="commit_a",
        trust_score=trust_score,
        pollution_score=0.0,
    )
    return cache, cache.add_pointer(pointer, level=level)


class HarnessCacheTests(unittest.TestCase):
    def test_search_opens_source_range_and_tracks_l1_holder(self) -> None:
        cache, _ = make_cache()

        results = cache.search(
            {
                "agent_id": "agent_a",
                "task": "jwt auth validation",
                "project_id": "default",
                "limit": 5,
                "required_tags": ["auth"],
            }
        )
        opened = cache.open("ptr_auth_001", agent_id="agent_a")

        self.assertEqual([pointer.pointer_id for pointer in results], ["ptr_auth_001"])
        self.assertIn("validate_jwt", opened.text)
        self.assertIn("agent_a", cache.coherence.holder_directory["ptr_auth_001"].holders)
        self.assertEqual(cache.l1["agent_a"]["ptr_auth_001"].coherence_state, CoherenceState.SHARED)

    def test_committed_pointer_update_invalidates_old_l1_holders(self) -> None:
        cache, pointer = make_cache()
        cache.search({"agent_id": "agent_b", "task": "jwt", "project_id": "default", "limit": 5})
        self.assertEqual(cache.l1["agent_b"]["ptr_auth_001"].pointer_version, 1)

        lease = cache.acquire_lease(
            {
                "agent_id": "agent_a",
                "pointer_id": "ptr_auth_001",
                "purpose": "reanchor",
                "ttl_ms": 30_000,
            }
        )
        proposed = pointer.copy(line_start=3, line_end=4, range_hash="")
        result = cache.commit_pointer_update(
            {
                "agent_id": "agent_a",
                "lease_id": lease.lease_id,
                "pointer_id": "ptr_auth_001",
                "base_pointer_version": 1,
                "new_pointer": proposed,
                "evidence": [
                    {
                        "agent_id": "agent_a",
                        "pointer_id": "ptr_auth_001",
                        "task_id": "task_update",
                        "evidence_type": "patch_linked",
                    }
                ],
            }
        )

        self.assertTrue(result.success)
        self.assertEqual(result.pointer.pointer_version, 2)
        self.assertEqual(cache.l1["agent_b"]["ptr_auth_001"].coherence_state, CoherenceState.INVALID)
        self.assertIn("POINTER_INVALIDATE", [event.event_type for event in result.events])
        with self.assertRaises(InvalidPointerError):
            cache.open("ptr_auth_001", agent_id="agent_b")

    def test_expired_lease_cannot_commit_modified_pointer(self) -> None:
        cache, pointer = make_cache()
        lease = cache.acquire_lease(
            {
                "agent_id": "agent_a",
                "pointer_id": "ptr_auth_001",
                "purpose": "edit",
                "ttl_ms": 30_000,
            }
        )
        cache.coherence.leases[lease.lease_id].expires_at = utcnow() - timedelta(seconds=1)

        result = cache.commit_pointer_update(
            {
                "agent_id": "agent_a",
                "lease_id": lease.lease_id,
                "pointer_id": "ptr_auth_001",
                "base_pointer_version": 1,
                "new_pointer": pointer.copy(title="changed"),
                "evidence": [],
            }
        )

        self.assertFalse(result.success)
        self.assertEqual(result.reason, "lease expired")
        self.assertEqual(cache.l3["ptr_auth_001"].pointer_version, 1)

    def test_source_change_marks_recoverable_pointer_owned_and_invalidates_l1(self) -> None:
        cache, _ = make_cache()
        cache.open("ptr_auth_001", agent_id="agent_a")
        changed_text = "\n".join(
            [
                "from tokens import decode",
                "# new helper inserted above the pointer",
                "",
                "def validate_jwt(refresh_token):",
                "    payload = decode(refresh_token)",
                "    return payload['sub']",
            ]
        )

        cache.handle_source_change(
            "repo_backend",
            "src/auth/middleware.py",
            changed_text,
            "commit_b",
            affected_ranges=[(1, 6)],
        )

        self.assertEqual(cache.l3["ptr_auth_001"].coherence_state, CoherenceState.OWNED)
        self.assertEqual(cache.l3["ptr_auth_001"].owner_service, "source_watcher")
        self.assertEqual(cache.l1["agent_a"]["ptr_auth_001"].coherence_state, CoherenceState.INVALID)
        with self.assertRaises(InvalidPointerError):
            cache.open("ptr_auth_001", agent_id="agent_a")

    def test_read_time_freshness_blocks_stale_pointer(self) -> None:
        cache, _ = make_cache()
        cache.sources.add("repo_backend", "src/auth/middleware.py", SOURCE_TEXT.replace("payload", "claims"), "commit_b")

        with self.assertRaises(StalePointerError):
            cache.open("ptr_auth_001", agent_id="agent_a")

        self.assertEqual(cache.l3["ptr_auth_001"].coherence_state, CoherenceState.INVALID)

    def test_trust_gated_promotion_requires_freshness_evidence_and_quorum(self) -> None:
        cache, _ = make_cache(level="candidate", trust_score=0.72)

        blocked = cache.evaluate_promotion("ptr_auth_001")
        self.assertFalse(blocked.promoted)
        self.assertEqual(blocked.to_state, QualityState.CANDIDATE)

        cache.record_evidence(
            {
                "agent_id": "agent_a",
                "pointer_id": "ptr_auth_001",
                "task_id": "task_1",
                "evidence_type": "opened",
            }
        )
        probation = cache.evaluate_promotion("ptr_auth_001")
        l2 = cache.evaluate_promotion("ptr_auth_001")

        self.assertTrue(probation.promoted)
        self.assertEqual(probation.to_state, QualityState.PROBATION)
        self.assertTrue(l2.promoted)
        self.assertIn("ptr_auth_001", cache.l2)

        cache.record_evidence(
            {
                "agent_id": "agent_a",
                "pointer_id": "ptr_auth_001",
                "task_id": "task_1",
                "evidence_type": "cited",
            }
        )
        cache.record_evidence(
            {
                "agent_id": "agent_b",
                "pointer_id": "ptr_auth_001",
                "task_id": "task_2",
                "evidence_type": "test_passed",
            }
        )
        hot = cache.evaluate_promotion("ptr_auth_001")

        self.assertTrue(hot.promoted)
        self.assertIn("ptr_auth_001", cache.hot_set)

    def test_metric_formulas_match_guidance_definitions(self) -> None:
        cache, _ = make_cache()
        cache.metrics.counters["tokens_with_cache"] = 50
        cache.metrics.counters["tokens_without_cache"] = 100
        cache.metrics.counters["stale_l1_hits"] = 1
        cache.metrics.counters["total_l1_hits"] = 20
        cache.metrics.counters["attempted_commits"] = 10
        cache.metrics.counters["version_conflict_commits"] = 2

        self.assertEqual(cache.metrics.token_io_reduction(), 0.5)
        self.assertEqual(cache.metrics.stale_l1_hit_rate(), 0.05)
        self.assertEqual(cache.metrics.version_conflict_rate(), 0.2)

    def test_monitoring_alerts_follow_guidance_thresholds(self) -> None:
        cache, _ = make_cache()
        cache.metrics.counters["invalid_pointer_uses"] = 2
        cache.metrics.counters["total_pointer_uses"] = 100
        cache.metrics.counters["drifted_pointers"] = 11
        cache.metrics.counters["verified_pointers"] = 100
        cache.metrics.observe_latency("ttfe", 20)
        cache.metrics.observe_latency("ttfe", 40)

        alerts = cache.metrics.alerts()

        self.assertIn("Invalid Pointer Usage Rate > 0.1%", alerts)
        self.assertIn("Pointer Drift Rate > 10%", alerts)
        self.assertEqual(cache.metrics.ttfe_p50(), 20)

    def test_cached_agent_workflow_opens_ranges_and_reports_token_savings(self) -> None:
        cache, _ = make_cache()
        workflow = CachedAgentWorkflow(cache)
        session = workflow.start_task(
            agent_id="agent_a",
            task="investigate jwt refresh token validation",
            task_id="task_agent_001",
        )

        ranges = session.find_evidence(limit=5, open_limit=1, required_tags=["auth"])
        pointer_id = ranges[0].pointer.pointer_id
        session.use(pointer_id)
        session.cite(pointer_id, correct=True)
        report = session.complete(success=True, supported_key_claims=1, total_key_claims=1)

        self.assertEqual(len(ranges), 1)
        self.assertIn("validate_jwt", ranges[0].source_range.text)
        self.assertGreater(report.tokens_without_cache, report.tokens_with_cache)
        self.assertGreater(report.token_io_reduction, 0)
        self.assertEqual(report.tool_calls_with_cache, 2)
        self.assertEqual(report.tool_calls_without_cache, 1)
        self.assertEqual(cache.metrics.counters["total_tasks"], 1)
        self.assertEqual(cache.metrics.counters["successful_tasks"], 1)
        self.assertEqual(cache.metrics.counters["correct_citations"], 1)
        self.assertGreaterEqual(cache.metrics.ttfe_p50(), 0)

    def test_cached_agent_workflow_skips_stale_pointer_without_claiming_saved_tokens(self) -> None:
        cache, _ = make_cache()
        cache.sources.add(
            "repo_backend",
            "src/auth/middleware.py",
            SOURCE_TEXT.replace("payload", "claims"),
            "commit_b",
        )
        workflow = CachedAgentWorkflow(cache)
        session = workflow.start_task("agent_a", "investigate jwt validation", task_id="task_agent_002")

        ranges = session.find_evidence(limit=5, open_limit=1)
        report = session.complete(success=False)

        self.assertEqual(ranges, [])
        self.assertEqual(session.skipped_pointer_ids, ["ptr_auth_001"])
        self.assertEqual(report.tokens_with_cache, 0)
        self.assertEqual(report.tokens_without_cache, 0)
        self.assertEqual(report.token_io_reduction, 0)
        self.assertEqual(cache.metrics.counters["stale_l1_hits"], 1)

    def test_compare_reports_aggregates_repeated_agent_token_savings(self) -> None:
        cache, _ = make_cache()
        workflow = CachedAgentWorkflow(cache)
        reports = []

        for agent_id in ("agent_a", "agent_b", "agent_c"):
            session = workflow.start_task(agent_id, "investigate jwt refresh token validation")
            ranges = session.find_evidence(limit=5, required_tags=["auth"])
            self.assertEqual(len(ranges), 1)
            session.use(ranges[0].pointer.pointer_id)
            reports.append(session.complete(success=True))

        comparison = compare_reports(reports)

        self.assertEqual(comparison.tasks, 3)
        self.assertTrue(comparison.saves_tokens)
        self.assertGreater(comparison.saved_tokens, 0)
        self.assertGreater(comparison.token_io_reduction, 0)

    def test_demo_token_savings_stays_positive(self) -> None:
        result = run_demo()

        self.assertEqual(result.no_cache_tokens, result.comparison.tokens_without_cache)
        self.assertEqual(result.cache_tokens, result.comparison.tokens_with_cache)
        self.assertTrue(result.comparison.saves_tokens)
        self.assertGreater(result.comparison.saved_tokens, 0)
        self.assertGreater(result.comparison.token_io_reduction, 0.80)

    def test_workflow_accepts_custom_token_estimator(self) -> None:
        cache, _ = make_cache()
        workflow = CachedAgentWorkflow(cache, token_estimator=lambda text: len(text.split()))
        session = workflow.start_task("agent_a", "investigate jwt refresh token validation")

        ranges = session.find_evidence(limit=5, required_tags=["auth"])
        report = session.complete(success=True)

        self.assertEqual(ranges[0].estimated_tokens, len(ranges[0].source_range.text.split()))
        self.assertEqual(report.tokens_with_cache, ranges[0].estimated_tokens)

    def test_explicit_no_cache_baseline_can_be_recorded_without_opening_pointer(self) -> None:
        cache, _ = make_cache()
        workflow = CachedAgentWorkflow(cache)
        session = workflow.start_task("agent_a", "manual baseline")

        baseline_tokens = session.record_no_cache_baseline("repo_backend", "src/auth/middleware.py")
        report = session.complete(success=False)

        self.assertGreater(baseline_tokens, 0)
        self.assertEqual(report.tokens_with_cache, 0)
        self.assertEqual(report.tokens_without_cache, baseline_tokens)


if __name__ == "__main__":
    unittest.main()
