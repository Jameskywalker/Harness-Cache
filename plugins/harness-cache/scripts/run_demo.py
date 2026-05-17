#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists() and (parent / "harness_cache").is_dir():
            return parent
    raise RuntimeError("Could not locate Harness Cache repository root.")


ROOT = repo_root()
sys.path.insert(0, str(ROOT))

from examples.demo_token_savings import TASKS, build_cache, run_demo
from harness_cache import CachedAgentWorkflow, compare_reports


def inspect_cache() -> dict:
    cache = build_cache()
    workflow = CachedAgentWorkflow(cache)
    reports = []
    for task in TASKS:
        session = workflow.start_task(task["agent_id"], task["task"])
        ranges = session.find_evidence(limit=5, required_tags=task["required_tags"])
        if ranges:
            pointer_id = ranges[0].pointer.pointer_id
            session.use(pointer_id)
            session.cite(pointer_id, correct=True)
            session.test_passed(pointer_id)
            reports.append(session.complete(success=True, supported_key_claims=1, total_key_claims=1))
        else:
            reports.append(session.complete(success=False))

    comparison = compare_reports(reports)
    return {
        "stores": {
            "l1_agents": sorted(cache.l1),
            "l2": sorted(cache.l2),
            "l3": sorted(cache.l3),
            "hot_set": sorted(cache.hot_set),
            "candidate_pool": sorted(cache.candidate_pool),
            "probation": sorted(cache.probation),
        },
        "pointers": {
            pointer_id: {
                "path": pointer.path,
                "lines": [pointer.line_start, pointer.line_end],
                "state": pointer.coherence_state.value,
                "trust_score": pointer.trust_score,
                "pollution_score": pointer.pollution_score,
                "quality_state": pointer.quality_state.value,
                "usage_count": pointer.usage_count,
                "tags": list(pointer.tags),
            }
            for pointer_id, pointer in sorted(cache.l3.items())
        },
        "l1": {
            agent_id: [
                f"{pointer_id}@v{pointer.pointer_version}:{pointer.coherence_state.value}"
                for pointer_id, pointer in l1.items()
            ]
            for agent_id, l1 in sorted(cache.l1.items())
        },
        "comparison": {
            "tokens_with_cache": comparison.tokens_with_cache,
            "tokens_without_cache": comparison.tokens_without_cache,
            "saved_tokens": comparison.saved_tokens,
            "token_io_reduction": comparison.token_io_reduction,
            "saves_tokens": comparison.saves_tokens,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or inspect the Harness Cache demo.")
    parser.add_argument("--inspect", action="store_true", help="Print demo cache state as JSON.")
    args = parser.parse_args()

    if args.inspect:
        print(json.dumps(inspect_cache(), indent=2, sort_keys=True))
        return

    result = run_demo()
    print("Harness Cache token savings demo")
    print(f"No cache estimated tokens: {result.no_cache_tokens}")
    print(f"Harness Cache estimated tokens: {result.cache_tokens}")
    print(f"Saved estimated tokens: {result.comparison.saved_tokens}")
    print(f"Token I/O reduction: {result.comparison.token_io_reduction:.1%}")
    print(f"Saves tokens: {result.comparison.saves_tokens}")


if __name__ == "__main__":
    main()
