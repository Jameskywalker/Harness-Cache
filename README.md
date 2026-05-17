# Harness Cache

This repository contains a compact Python implementation of the design in
`harness_cache_multi_agent_guidance_en.md`.

The implementation is intentionally in-memory and standard-library first. It is
meant to prove the protocol and API shape before adding durable storage,
connectors, or a service boundary.

## Implemented

- Pointer schema with source location, anchors, versions, HC-MOESI state, trust,
  pollution, freshness, and quality metadata.
- Agent-private L1 caches and shared L2/L3 stores.
- Directory-based holder tracking.
- Write leases for pointer updates.
- WAL/event log for proposed updates, commits, invalidations, source changes,
  and evidence.
- Read-time source version and range hash freshness checks.
- Directory-based invalidation and event replay after an agent restart.
- Source-change handling with Owned/Invalid transitions.
- Trust-gated promotion from candidate/probation to L2 and hot set.
- Metrics for efficiency, coherence, freshness, cache quality, correctness, and
  multi-agent health.

## Quick Start

```python
from harness_cache import CachedAgentWorkflow, HarnessCache, Pointer, compare_reports

cache = HarnessCache()
cache.add_source("repo", "app.py", "def main():\n    return 1\n", "commit_a")

pointer = Pointer(
    pointer_id="ptr_main",
    source_id="repo",
    path="app.py",
    line_start=1,
    line_end=2,
    anchors=("main",),
    tags=("python",),
    source_version="commit_a",
    trust_score=0.6,
)
cache.add_pointer(pointer, level="l2")

results = cache.search({
    "agent_id": "agent_a",
    "task": "find python main",
    "project_id": "default",
    "limit": 5,
})

source_range = cache.open("ptr_main", agent_id="agent_a")
```

## Agent Workflow

Use `CachedAgentWorkflow` at the start of an agent task. It searches cache
pointers first, opens only targeted source ranges, records evidence, and updates
token/tool-call metrics against a full-source-read baseline.

```python
workflow = CachedAgentWorkflow(cache)
session = workflow.start_task("agent_a", "find python main")
ranges = session.find_evidence(limit=5, open_limit=2)

for evidence_range in ranges:
    session.use(evidence_range.pointer.pointer_id)
    session.cite(evidence_range.pointer.pointer_id)

report = session.complete(success=True, supported_key_claims=1, total_key_claims=1)
comparison = compare_reports([report])
print(comparison.saved_tokens, comparison.token_io_reduction)
```

## Token Savings Demo

Run the deterministic repeated-agent demo:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 examples/demo_token_savings.py
```

Expected result: Harness Cache should report fewer estimated tokens than the
no-cache full-file baseline and `Saves tokens: True`.

## Test

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```
