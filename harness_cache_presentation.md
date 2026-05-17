# Harness Cache Demo

## What Harness Cache Does

Harness Cache reduces repeated context reading by caching **source pointers**.

It does not cache facts or summaries as truth. Instead, it caches structured
hints that tell an agent where to verify evidence in the original source.

```text
Cache where to look, not what to believe.
Original source files remain the source of truth.
```

## Example Pointer

```text
ptr_orders_reserve
source: random_shop
path: orders.py
lines: 1-4
anchors: reserve_inventory, retry='short'
tags: orders, inventory, retry
```

This pointer tells the agent: “for inventory reservation behavior, inspect
`orders.py` lines 1-4.”

## Cache Layout

Harness Cache uses:

- **Agent-private L1**: small local cache for each agent.
- **Shared L2**: trusted project-level pointer cache.
- **Shared L3**: broader pointer index.
- **Holder directory**: tracks which agents hold which pointer versions.
- **Event log**: records pointer adds, opens, evidence, commits, and invalidations.

## Example L1 Entry

```text
agent_orders: ptr_orders_reserve@v1:S
```

Meaning:

- `agent_orders`: the agent that owns this private L1 entry.
- `ptr_orders_reserve`: the pointer ID.
- `@v1`: pointer version 1.
- `S`: shared state, meaning it is currently safe to read.

If the pointer is updated elsewhere, this old local copy can be invalidated:

```text
ptr_orders_reserve@v1:I
```

`I` means invalid, so the agent must not inject it into a prompt.

## Coherence States

Harness Cache uses an HC-MOESI-style state model:

| State | Meaning |
|---|---|
| `M` | Modified locally but not committed |
| `O` | Owned by an agent/service for revalidation |
| `E` | Exclusive write lease granted |
| `S` | Shared and readable |
| `I` | Invalid, stale, or unverifiable |

## Demo Setup

The demo project is a deterministic small “random shop” codebase:

```text
orders.py
payments.py
notifications.py
```

Each file contains a small useful source range plus many filler lines. This
simulates a real project where agents often reread whole files to find a small
piece of evidence.

The demo runs repeated agent tasks:

```text
agent_orders         -> inventory reservation behavior
agent_payments       -> payment capture idempotency behavior
agent_notifications  -> receipt delivery template behavior
agent_orders_2       -> repeated inventory reservation lookup
```

## No-Cache Baseline

Without Harness Cache, each agent reads the full source file for its task.

```text
No cache estimated tokens: 6487
```

## Harness Cache Path

With Harness Cache, each agent:

1. Searches shared pointers.
2. Opens only the targeted source range.
3. Records evidence such as `opened`, `used`, `cited`, and `test_passed`.
4. Promotes useful trusted pointers.

```text
Harness Cache estimated tokens: 149
```

## Result

```text
Saved estimated tokens: 6338
Token I/O reduction: 97.7%
Saves tokens: True
```

## Cache After Demo

Shared stores:

```text
L2: ptr_notifications_receipt, ptr_orders_reserve, ptr_payments_capture
L3: ptr_notifications_receipt, ptr_orders_reserve, ptr_payments_capture
Hot set: ptr_orders_reserve
```

Agent-private L1 caches:

```text
agent_orders:         ptr_orders_reserve@v1:S
agent_orders_2:       ptr_orders_reserve@v1:S
agent_payments:       ptr_payments_capture@v1:S
agent_notifications:  ptr_notifications_receipt@v1:S
```

Evidence recorded:

```text
ptr_orders_reserve:        seen, opened, used, cited, test_passed x2
ptr_payments_capture:      seen, opened, used, cited, test_passed
ptr_notifications_receipt: seen, opened, used, cited, test_passed
```

## Why It Saves Tokens

The cache avoids rereading large files when a small verified range is enough.

Instead of putting an entire source file into context, the agent receives only
the relevant lines and metadata needed to verify the answer.

## Important Limitations

This implementation is currently an in-memory prototype:

- cache state is not persisted across Python processes;
- token counting uses a deterministic estimator, not a model tokenizer;
- source pointers must be seeded or discovered before they can help;
- bad or stale pointers must be invalidated to avoid false savings.

## How to Run

```bash
PYTHONDONTWRITEBYTECODE=1 python3 examples/demo_token_savings.py
```

Expected output:

```text
Harness Cache token savings demo
No cache estimated tokens: 6487
Harness Cache estimated tokens: 149
Saved estimated tokens: 6338
Token I/O reduction: 97.7%
Saves tokens: True
```

## Takeaway

Harness Cache is useful when agents repeatedly inspect the same project files,
docs, or notes. It saves tokens by routing agents to verifiable source ranges
instead of repeatedly loading broad context.
