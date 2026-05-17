---
name: harness-cache
description: Use Harness Cache to reduce repeated context reads with source pointers, run the token-savings demo, inspect cache contents, or explain pointer notation such as `ptr_id@v1:S`.
---

# Harness Cache

Use this skill when the user asks to:

- reduce token use during repeated codebase or document investigations;
- compare cache versus no-cache token usage;
- inspect Harness Cache stores, L1 entries, evidence, or metrics;
- explain pointer notation such as `ptr_orders_reserve@v1:S`;
- run or present the Harness Cache demo.

## Core Idea

Harness Cache stores **source pointers**, not facts:

```text
Cache where to look, not what to believe.
Original source files remain the source of truth.
```

Each pointer routes an agent to a verifiable source range with path, line range,
anchors, source version, range hash, trust score, and coherence state.

## Workflow

1. Search cache pointers before broad source reads.
2. Open only targeted source ranges when pointers are fresh and readable.
3. Record evidence such as `opened`, `used`, `cited`, `patch_linked`, or `test_passed`.
4. Compare `tokens_with_cache` against an explicit no-cache full-read baseline.
5. Invalidate stale pointers instead of injecting them into prompts.

## Scripts

Run the repository demo through the plugin wrapper:

```bash
python3 plugins/harness-cache/scripts/run_demo.py
```

Print the demo cache state:

```bash
python3 plugins/harness-cache/scripts/run_demo.py --inspect
```

Measure real Codex CLI token usage by running one cached prompt and one
no-cache prompt through `codex exec`:

```bash
python3 plugins/harness-cache/scripts/run_demo.py --measure-codex --dry-run
python3 plugins/harness-cache/scripts/run_demo.py --measure-codex --yes
python3 plugins/harness-cache/scripts/run_demo.py --measure-codex --yes --json-output
```

The measurement reads completed-turn usage from `~/.codex/log/codex-tui.log`.

Use `PYTHONDONTWRITEBYTECODE=1` when running tests or demos if you want to avoid
creating `__pycache__` directories.

## Pointer Notation

```text
agent_orders: ptr_orders_reserve@v1:S
```

Meaning:

- `agent_orders`: the agent-private L1 owner.
- `ptr_orders_reserve`: pointer ID.
- `@v1`: pointer version 1.
- `S`: shared readable state.

Readable states are `S` and `E`. Invalid pointers use `I` and must not be
injected into prompts.

## Verification

For repository changes, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 examples/demo_token_savings.py
PYTHONDONTWRITEBYTECODE=1 python3 plugins/harness-cache/scripts/run_demo.py --inspect
PYTHONDONTWRITEBYTECODE=1 python3 plugins/harness-cache/scripts/run_demo.py --measure-codex --dry-run
```
