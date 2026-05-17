# Harness Cache Multi-Agent Design Guide

> Goal: design a stable multi-agent cache protocol for Harness Cache. The recommended architecture is **agent-private L1 + shared L2/L3 + directory-based snoop/invalidation + HC-MOESI + trust-gated promotion**. The system reduces context waste while controlling pointer drift, cache pollution, and concurrent updates from multiple agents.

---

## 1. Project Positioning

Harness Cache is not a traditional summarization system and it is not ordinary RAG. Its core idea is to cache **source pointers**: structured hints that tell an agent *where to verify evidence*, not *what to believe*.

A pointer usually looks like this:

```json
{
  "pointer_id": "ptr_auth_001",
  "source_id": "repo_backend",
  "source_type": "code",
  "path": "src/auth/middleware.ts",
  "title": "JWT refresh-token validation",
  "line_start": 42,
  "line_end": 88,
  "anchors": ["validateJWT", "refreshToken"],
  "tags": ["auth", "jwt", "middleware"],
  "source_version": "commit_abc123",
  "range_hash": "hash_of_referenced_text",
  "pointer_version": 12,
  "coherence_state": "S",
  "trust_score": 0.74,
  "pollution_score": 0.08,
  "last_verified_at": "2026-05-17T12:00:00Z"
}
```

Core principles:

```text
Cache where to look, not what to believe.
Original sources remain the source of truth.
Pointers are routing hints, not facts.
A pointer must be verifiable, versioned, and invalidatable.
```

---

## 2. Design Goals

### 2.1 Primary Goals

1. Reduce token waste caused by repeatedly reading long documents, repositories, or project notes.
2. Help agents reach original evidence faster.
3. Keep pointer state coherent in multi-agent environments.
4. Prevent stale, drifted, or misleading pointers from polluting hot caches.
5. Make cache promotion, demotion, and eviction explainable and auditable.
6. Preserve traceability: final answers and code changes should be linked back to source ranges.

### 2.2 Non-Goals

Harness Cache should not directly replace:

- full-text search;
- vector databases;
- document summarization;
- an agent's final reasoning or judgment;
- authorization, security, or compliance systems.

Harness Cache should act as a **context-routing layer** inside agent workflows.

---

## 3. Recommended High-Level Architecture

```text
                 ┌──────────────────────┐
                 │        Agent A        │
                 │      Private L1       │
                 └──────────┬───────────┘
                            │
                 ┌──────────▼───────────┐
                 │        Agent B        │
                 │      Private L1       │
                 └──────────┬───────────┘
                            │
                 ┌──────────▼───────────┐
                 │        Agent C        │
                 │      Private L1       │
                 └──────────┬───────────┘
                            │
                            ▼
             ┌──────────────────────────────┐
             │      Coherence Manager        │
             │  - holder directory           │
             │  - leases / locks             │
             │  - snoop invalidation         │
             │  - version checks             │
             └──────────────┬───────────────┘
                            │
                            ▼
             ┌──────────────────────────────┐
             │      Shared L2 / L3 Store     │
             │  - pointer index              │
             │  - trust scores               │
             │  - freshness metadata         │
             │  - promotion evidence         │
             └──────────────┬───────────────┘
                            │
                            ▼
             ┌──────────────────────────────┐
             │       Source Watcher          │
             │  - git commit changes         │
             │  - doc updates                │
             │  - ticket/note modifications  │
             └──────────────┬───────────────┘
                            │
                            ▼
             ┌──────────────────────────────┐
             │        Event Log / WAL        │
             │  - pointer updates            │
             │  - invalidation events        │
             │  - source-change events       │
             │  - promotion/demotion events  │
             └──────────────────────────────┘
```

The key architectural choice is that **each agent owns its private L1**, while shared L2/L3 acts as the durable, auditable pointer store. The Coherence Manager coordinates versioning, invalidation, leases, and recovery.

---

## 4. Cache Levels

### 4.1 Agent-Private L1

Each agent maintains its own L1 cache.

L1 characteristics:

- small;
- hot;
- local to the agent;
- optimized for the current task;
- contains recently used or task-relevant pointers;
- should not be treated as global truth.

Recommended size:

```text
MVP: 20-50 pointers per agent
Production: dynamically tuned, for example 20-200 pointers per agent depending on task type
```

### 4.2 Shared L2

L2 is the project-level shared pointer cache.

Use L2 for:

- pointers validated across multiple tasks or agents;
- the main refill source for L1;
- trust scores, usage history, freshness metadata, and promotion evidence;
- medium-term project memory.

### 4.3 Shared L3

L3 is the broad global pointer index.

Use L3 for:

- low-frequency but broad-coverage pointers;
- tag, keyword, embedding, and source-graph retrieval;
- fallback search when L1/L2 misses;
- cross-project or long-tail evidence routing.

---

## 5. HC-MOESI Pointer Coherence Protocol

Harness Cache can borrow the structure of MOESI, but the meaning should be adapted for pointer coherence rather than CPU memory coherence.

### 5.1 State Definitions

| State | Name | Meaning | Directly usable? |
|---|---|---|---|
| M | Modified | Current agent changed the pointer but has not committed it to shared cache | No, except internally by the modifying agent |
| O | Owned | An agent or service owns revalidation / re-anchoring responsibility | Cautiously, only after lightweight validation |
| E | Exclusive | Current agent has exclusive write rights for this pointer | Yes |
| S | Shared | Multiple agents may read this pointer | Yes |
| I | Invalid | Pointer is stale, drifted, unverifiable, or explicitly invalidated | No |

### 5.2 Common State Transitions

| Scenario | Transition |
|---|---|
| Agent reads shared pointer | `S -> S` |
| Agent requests write rights | `S -> E` |
| Agent begins editing pointer | `E -> M` |
| Edit is validated and committed | `M -> S` |
| Edit fails validation | `M -> I` |
| Source changed but pointer may be recoverable | `S -> O` |
| Re-anchor succeeds | `O -> S` |
| Re-anchor fails | `O -> I` |
| Pointer is clearly stale or contradicted | `S/E/O -> I` |
| Pointer is rediscovered and verified | `I -> S` |

### 5.3 Core Coherence Rules

```text
1. I-state pointers must never be injected directly into an agent prompt.
2. M-state pointers must not be readable by other agents.
3. E-state pointers must be protected by a lease.
4. O-state pointers must have an owner_agent or owner_service.
5. S-state pointers are readable, but pointer_version and source_version must still be checked.
6. Every M -> S commit must be written to the WAL/event log.
7. Core location fields should use write-invalidate, not write-update.
```

---

## 6. Snoop / Invalidation Design

### 6.1 Recommended Strategy

Do not use pure broadcast snooping as the default. Use:

```text
Directory-based invalidation
+ event log replay
+ lightweight read-time version checks
```

The Coherence Manager maintains a holder directory:

```json
{
  "ptr_auth_001": {
    "pointer_version": 12,
    "holders": ["agent_A", "agent_B", "agent_C"],
    "state": "S"
  }
}
```

When a pointer changes, only agents that hold the old pointer need to be notified.

### 6.2 Invalidation Event

```json
{
  "event_type": "POINTER_INVALIDATE",
  "pointer_id": "ptr_auth_001",
  "old_pointer_version": 12,
  "new_pointer_version": 13,
  "source_id": "repo_backend",
  "source_version": "commit_def456",
  "reason": "reanchored_by_agent_A",
  "seq": 8813,
  "created_at": "2026-05-17T12:30:00Z"
}
```

Receiver behavior:

```text
if local_L1 contains pointer_id and local.pointer_version <= old_pointer_version:
    local.coherence_state = I
    local.invalidated_at = event.created_at
```

### 6.3 Why Core Fields Should Use Write-Invalidate

Core pointer-location fields include:

```text
path
line_start
line_end
anchors
range_hash
source_version
pointer_version
```

For these fields, prefer write-invalidate over write-update because:

- the new pointer should often be fetched and verified by the receiving agent;
- an incorrect re-anchor should not be broadcast as a new fact;
- invalidation is simpler to audit and debug;
- stale local state is easier to detect.

### 6.4 Fields That May Use Write-Update

Some non-critical metadata can be updated directly:

```text
usage_count
last_used_at
last_verified_at
minor trust_score updates
```

However, promotion, anchor, range, and source-version changes should still trigger invalidation.

---

## 7. Standard Pointer Update Flow

### 7.1 Agent Update Flow

```text
1. Agent wants to modify a pointer.
2. Agent requests a lease from the Coherence Manager.
3. If the lease is granted: S -> E.
4. Agent modifies the pointer: E -> M.
5. Agent validates source_version, range_hash, and anchor stability.
6. Agent writes the proposed update to the WAL.
7. Coherence Manager verifies lease, base version, and source version.
8. If valid: commit to L2/L3, then M -> S.
9. Coherence Manager sends invalidation events to old holders.
10. Other agents mark their local L1 pointer as I.
```

### 7.2 Lease Object

```json
{
  "lease_id": "lease_789",
  "pointer_id": "ptr_auth_001",
  "holder_agent": "agent_A",
  "lease_state": "active",
  "granted_at": "2026-05-17T12:00:00Z",
  "expires_at": "2026-05-17T12:05:00Z",
  "purpose": "reanchor"
}
```

### 7.3 Lease Rules

```text
1. A pointer can have only one active write lease at a time.
2. An expired lease cannot commit an M-state pointer.
3. Changes from expired leases must revalidate source_version before retrying.
4. Leases should not block ordinary reads.
5. Pointer-edit leases and promotion leases may be separate.
```

---

## 8. Handling Source Changes

### 8.1 Source Watcher

The Source Watcher monitors:

```text
git commit changes
file hash changes
document modified_at changes
section hash changes
ticket/comment updates
note version changes
```

### 8.2 Source Change Event

```json
{
  "event_type": "SOURCE_CHANGED",
  "source_id": "repo_backend",
  "path": "src/auth/middleware.ts",
  "old_source_version": "commit_abc123",
  "new_source_version": "commit_def456",
  "affected_ranges": [[40, 100]],
  "seq": 9001,
  "created_at": "2026-05-17T12:40:00Z"
}
```

### 8.3 Handling Strategy

```text
1. Find affected pointers.
2. If anchor/symbol/heading can still be located, mark the pointer as O.
3. If range_hash does not match and anchors are unstable, mark the pointer as I.
4. Notify holder agents with invalidation events.
5. On next access, trigger re-anchor or rediscovery.
```

---

## 9. Preventing Pointer Drift

### 9.1 Do Not Rely Only on Line Numbers

Each pointer should include multiple anchoring signals whenever possible:

```text
line range
heading anchor
symbol name
function/class name
AST node id
text hash
semantic fingerprint
nearby context hash
```

### 9.2 Recommended Re-Anchor Order

```text
1. Exact anchor match
2. Symbol / heading match
3. Range hash match
4. Nearby context fuzzy match
5. Semantic match
6. Fallback to full source search
7. Mark Invalid
```

### 9.3 Drift Detection

A pointer can be treated as drifted when:

```text
source_version changed
and range_hash mismatched
and anchor_confidence < threshold
```

Recommended fields:

```json
{
  "anchor_confidence": 0.82,
  "drift_score": 0.17,
  "reanchor_attempts": 2,
  "last_reanchor_result": "success"
}
```

---

## 10. Preventing Cache Pollution

### 10.1 Distinguish Evidence Strength

Do not promote a pointer just because it was retrieved or opened.

| Signal | Meaning | Useful for promotion? |
|---|---|---|
| Seen | Retrieved by search | No |
| Opened | Opened by an agent | Weak signal |
| Used | Used during reasoning or intermediate work | Medium signal |
| Cited | Cited in final answer | Strong signal |
| Patch-linked | Connected to a real code change | Strong signal |
| Test-passed | Used in a task whose tests passed | Strong signal |
| User-accepted | User accepted the result | Strong signal |

### 10.2 Admission Pipeline

```text
Candidate Pool -> Probation -> Shared L2 -> Global Hot Set -> Agent L1 Seed
```

Suggested rules:

```text
Candidate Pool:
    Newly discovered pointers enter here by default.

Probation:
    Requires freshness validation plus opened/used evidence.

Shared L2:
    Requires at least one strong evidence signal, or weak evidence from two independent tasks.

Global Hot Set:
    Requires multiple-agent or multiple-task validation, or citation/patch/test evidence.

Agent L1:
    Filled dynamically using current task relevance, hotness, trust, and freshness.
```

### 10.3 Promotion Gate

Suggested promotion criteria:

```text
L3 -> L2:
    freshness_valid == true
    and trust_score >= 0.45
    and pollution_score <= 0.35

L2 -> Global Hot Set:
    trust_score >= 0.70
    and stale_hit_rate <= 0.05
    and at least one strong_evidence

Global Hot Set -> L1 seed:
    task_relevance >= 0.70
    and coherence_state in {S, E}
    and source_version is fresh
```

### 10.4 Demotion and Eviction

Demote or evict when:

```text
opened but unused repeatedly
citation contradicted by source
task failed after using pointer
source became stale
re-anchor failed
agent/user marked it irrelevant
high conflict rate
```

Suggested quality states:

```text
Clean
Probation
Suspect
Quarantined
Invalid
```

---

## 11. Suggested API Draft

### 11.1 Search

```ts
cache.search({
  agent_id: string,
  task: string,
  project_id: string,
  limit: number,
  required_tags?: string[],
  exclude_invalid?: boolean
}): Pointer[]
```

### 11.2 Open Pointer

```ts
cache.open(pointer_id: string, options?: {
  verify_freshness?: boolean,
  max_staleness_ms?: number
}): SourceRange
```

### 11.3 Acquire Lease

```ts
cache.acquireLease({
  agent_id: string,
  pointer_id: string,
  purpose: "edit" | "reanchor" | "promotion",
  ttl_ms: number
}): Lease
```

### 11.4 Commit Pointer Update

```ts
cache.commitPointerUpdate({
  agent_id: string,
  lease_id: string,
  pointer_id: string,
  base_pointer_version: number,
  new_pointer: Pointer,
  evidence: Evidence[]
}): CommitResult
```

### 11.5 Record Evidence

```ts
cache.recordEvidence({
  agent_id: string,
  pointer_id: string,
  task_id: string,
  evidence_type: "opened" | "used" | "cited" | "patch_linked" | "test_passed" | "user_accepted" | "failed",
  weight?: number,
  metadata?: Record<string, unknown>
}): void
```

---

## 12. Evaluation Metric Categories

Evaluate Harness Cache across six categories:

```text
Efficiency
Coherence
Freshness / Drift
Cache Quality
Correctness
Multi-Agent Health
```

---

## 13. Efficiency Metrics

### 13.1 Token I/O Reduction

Measures how much context reading is reduced.

```text
Token I/O Reduction = 1 - tokens_with_cache / tokens_without_cache
```

Suggested targets:

```text
MVP: >= 30%
Production: >= 50%
Excellent: >= 70%
```

### 13.2 Full-Document Read Avoidance Rate

Measures how often the agent avoids reading full documents or broad repository context.

```text
Full-document Read Avoidance Rate = avoided_full_reads / total_tasks
```

Suggested targets:

```text
MVP: >= 40%
Production: >= 70%
```

### 13.3 Time to First Relevant Evidence, TTFE

Time from task start to the first useful source range being opened.

```text
TTFE = timestamp(first_relevant_pointer_opened) - timestamp(task_started)
```

Track:

```text
p50 TTFE
p95 TTFE
TTFE with cache vs without cache
```

### 13.4 Tool Call Reduction

```text
Tool Call Reduction = 1 - tool_calls_with_cache / tool_calls_without_cache
```

Suggested targets:

```text
MVP: >= 20%
Production: >= 40%
```

---

## 14. Coherence Metrics

### 14.1 Stale L1 Hit Rate

Percentage of L1 hits that were stale.

```text
Stale L1 Hit Rate = stale_L1_hits / total_L1_hits
```

Suggested targets:

```text
MVP: <= 5%
Production: <= 2%
Strict environments: <= 1%
```

### 14.2 Invalid Pointer Usage Rate

Percentage of actually used pointers that were in the Invalid state.

```text
Invalid Pointer Usage Rate = invalid_pointer_uses / total_pointer_uses
```

Suggested targets:

```text
Should be near 0
Warning threshold: > 0.1%
Critical threshold: > 1%
```

### 14.3 Invalidation Latency

Delay between a pointer/source update and invalidation of affected agent L1 entries.

```text
Invalidation Latency = timestamp(agent_L1_invalidated) - timestamp(commit_or_source_change)
```

Track:

```text
p50
p95
p99
```

Suggested targets:

```text
MVP p95: <= 5s
Production p95: <= 1s
```

### 14.4 Missed Snoop Rate

Percentage of invalidation events that should have been processed but were missed or delayed beyond the SLA.

```text
Missed Snoop Rate = missed_snoop_events / expected_snoop_events
```

Suggested targets:

```text
MVP: <= 1%
Production: <= 0.1%
```

### 14.5 Version Conflict Rate

Rate of commit attempts that conflict with pointer_version or source_version.

```text
Version Conflict Rate = version_conflict_commits / attempted_commits
```

Suggested targets:

```text
MVP: <= 10%
Production: <= 3%
```

---

## 15. Freshness / Drift Metrics

### 15.1 Pointer Drift Rate

Percentage of verified pointers whose line range, anchor, or hash no longer matches the intended source range.

```text
Pointer Drift Rate = drifted_pointers / verified_pointers
```

Suggested targets:

```text
MVP: <= 10%
Production: <= 3%
```

### 15.2 Re-Anchor Success Rate

Percentage of drifted pointers that can be successfully relocated.

```text
Re-anchor Success Rate = successful_reanchors / attempted_reanchors
```

Suggested targets:

```text
MVP: >= 60%
Production: >= 80%
Excellent: >= 90%
```

### 15.3 Source Change Detection Latency

Time from source modification to detection by the cache system.

```text
Source Change Detection Latency = detected_at - source_changed_at
```

Suggested target:

```text
Repo/git scenarios: seconds to minutes after commit
External docs: set SLA based on connector capability
```

### 15.4 Fresh Pointer Ratio

Percentage of active pointers that are fresh.

```text
Fresh Pointer Ratio = fresh_pointers / active_pointers
```

Suggested targets:

```text
MVP: >= 80%
Production: >= 90%
```

---

## 16. Cache Quality Metrics

### 16.1 Useful L1 Hit Rate

Percentage of L1 hits that become Used, Cited, or Patch-linked.

```text
Useful L1 Hit Rate = useful_L1_hits / total_L1_hits
```

Suggested targets:

```text
MVP: >= 30%
Production: >= 50%
Excellent: >= 70%
```

### 16.2 Pointer Precision@K

Percentage of the top K returned pointers that are truly relevant.

```text
Precision@K = relevant_pointers_in_top_K / K
```

Track:

```text
Precision@5
Precision@10
Precision@20
```

Suggested targets:

```text
P@5 MVP: >= 0.60
P@5 Production: >= 0.80
```

### 16.3 Pointer Recall@K

Percentage of the gold evidence set that appears in the top K pointers.

```text
Recall@K = gold_pointers_found_in_top_K / total_gold_pointers
```

Use this mainly for offline benchmarks.

### 16.4 Pollution Rate

Percentage of promoted pointers that later turn out to be useless, wrong, stale, or misleading.

```text
Pollution Rate = polluted_promoted_pointers / promoted_pointers
```

Suggested targets:

```text
MVP: <= 10%
Production: <= 5%
Strict environments: <= 2%
```

### 16.5 False Promotion Rate

Percentage of promoted pointers that never produce useful evidence or that contribute to failed tasks.

```text
False Promotion Rate = false_promotions / total_promotions
```

Suggested targets:

```text
MVP: <= 15%
Production: <= 5%
```

### 16.6 Eviction Regret Rate

Percentage of evicted pointers that are soon rediscovered and proven useful.

```text
Eviction Regret Rate = useful_evicted_pointers_reintroduced / total_evictions
```

Suggested targets:

```text
MVP: <= 10%
Production: <= 5%
```

### 16.7 Hot Set Churn

Measures whether the global hot set changes too aggressively.

```text
Hot Set Churn = changed_hot_entries_per_window / total_hot_entries
```

Guidance:

```text
If churn exceeds 30% per day in a stable project, promotion rules are probably too aggressive.
```

---

## 17. Correctness Metrics

### 17.1 Citation Accuracy

Whether cited pointers actually support the claims they are attached to.

```text
Citation Accuracy = correct_citations / total_citations
```

Suggested targets:

```text
MVP: >= 80%
Production: >= 90%
Strict environments: >= 95%
```

### 17.2 Task Success Rate

Task completion rate when Harness Cache is enabled.

```text
Task Success Rate = successful_tasks / total_tasks
```

Compare against a no-cache baseline:

```text
Task Success Lift = success_rate_with_cache - success_rate_without_cache
```

### 17.3 Evidence Coverage

Percentage of key claims or actions that are backed by source pointers.

```text
Evidence Coverage = supported_key_claims / total_key_claims
```

Suggested targets:

```text
MVP: >= 70%
Production: >= 85%
```

### 17.4 Contradicted Pointer Rate

Percentage of used pointers later found to conflict with the original source or higher-confidence evidence.

```text
Contradicted Pointer Rate = contradicted_used_pointers / used_pointers
```

Suggested targets:

```text
MVP: <= 5%
Production: <= 2%
```

---

## 18. Multi-Agent Health Metrics

### 18.1 Lease Contention Rate

Measures how often multiple agents compete for write rights to the same pointer.

```text
Lease Contention Rate = contested_lease_requests / total_lease_requests
```

Guidance:

```text
If this remains high, task partitioning or pointer granularity may be wrong.
```

### 18.2 Duplicate Work Rate

Measures repeated verification, re-anchoring, or retrieval work across agents.

```text
Duplicate Work Rate = duplicate_pointer_work_units / total_pointer_work_units
```

Suggested targets:

```text
MVP: <= 20%
Production: <= 10%
```

### 18.3 Agent L1 Divergence

Measures how often agents hold different versions of the same pointer.

```text
Agent L1 Divergence = divergent_pointer_copies / shared_pointer_copies
```

Suggested targets:

```text
MVP: <= 5%
Production: <= 1%
```

### 18.4 Event Replay Lag

How far behind an agent is in processing the event log.

```text
Event Replay Lag = latest_global_event_seq - agent_last_processed_seq
```

Alert when:

```text
one agent's lag grows continuously
many agents lag at the same time
lag correlates with stale L1 hits
```

### 18.5 Quorum Promotion Agreement

Agreement level among agents or independent tasks when promoting a pointer.

```text
Quorum Promotion Agreement = agreeing_agents / voting_agents
```

Suggested rules:

```text
L2 -> Global Hot Set:
    at least 2 agents or 2 independent tasks confirm usefulness.

High-risk projects:
    require at least 3 independent evidence signals.
```

---

## 19. Composite Health Score

A useful overall metric is the Harness Cache Health Score:

```text
Health Score =
  0.20 * Efficiency Score
+ 0.20 * Coherence Score
+ 0.15 * Freshness Score
+ 0.20 * Cache Quality Score
+ 0.15 * Correctness Score
+ 0.10 * Multi-Agent Health Score
```

Normalize each sub-score to 0-100.

Example mapping:

```text
Efficiency Score:
    token reduction, tool call reduction, TTFE improvement

Coherence Score:
    stale hit rate, invalid usage rate, invalidation latency

Freshness Score:
    drift rate, re-anchor success, fresh pointer ratio

Cache Quality Score:
    useful L1 hit rate, Precision@K, pollution rate

Correctness Score:
    citation accuracy, task success lift, evidence coverage

Multi-Agent Health Score:
    lease contention, duplicate work, event lag, divergence
```

Suggested grade bands:

```text
90-100: Excellent
75-89: Healthy
60-74: Needs tuning
40-59: Risky
0-39: Unsafe for production
```

---

## 20. Benchmark Design

### 20.1 Offline Benchmark

Prepare tasks such as:

```text
code debugging tasks
document QA tasks
architecture lookup tasks
long project-note retrieval tasks
multi-agent parallel investigation tasks
source-change / pointer-drift tasks
```

Each task should have gold evidence:

```json
{
  "task_id": "task_auth_bug_001",
  "gold_pointers": ["ptr_auth_001", "ptr_auth_007"],
  "expected_sources": [
    "src/auth/middleware.ts:42-88",
    "docs/security.md#refresh-token-policy"
  ],
  "success_criteria": [
    "identifies expired refresh token path",
    "cites policy section",
    "does not rely on stale implementation note"
  ]
}
```

### 20.2 Online A/B Test

Compare:

```text
A: no cache
B: simple RAG
C: Harness Cache without coherence
D: Harness Cache with HC-MOESI + snoop
```

Track:

```text
token I/O
tool calls
TTFE
task success
citation accuracy
stale hit rate
pollution rate
```

### 20.3 Stress Tests

Simulate:

```text
10 agents concurrently reading the same repo
multiple agents re-anchoring the same pointer
frequent source file changes
event log delays
agent offline and recovery
incorrect pointer promotion
large numbers of irrelevant candidate pointers
```

---

## 21. Monitoring and Alerts

Monitor at least the following alerts:

```text
Invalid Pointer Usage Rate > 0.1%
Stale L1 Hit Rate > 5%
Missed Snoop Rate > 1%
Pointer Drift Rate > 10%
Pollution Rate > 10%
Citation Accuracy < 80%
Event Replay Lag keeps increasing
Lease Contention Rate spikes abnormally
Re-anchor Success Rate < 60%
```

---

## 22. MVP Implementation Roadmap

### Phase 1: Single-Agent Pointer Cache

Goal: prove that pointer caching saves context.

Features:

```text
pointer schema
source range open
basic search
usage logging
freshness check
```

Metrics:

```text
Token I/O Reduction
TTFE
Useful L1 Hit Rate
Precision@K
```

### Phase 2: Multi-Agent Private L1 + Shared L2

Goal: let multiple agents maintain private L1 caches while sharing trusted pointers.

Features:

```text
agent-local L1
shared L2 store
holder directory
pointer_version
source_version
```

Metrics:

```text
Agent L1 Divergence
Duplicate Work Rate
Stale L1 Hit Rate
```

### Phase 3: HC-MOESI + Lease + Invalidation

Goal: handle concurrent updates and pointer drift propagation.

Features:

```text
coherence_state
lease manager
write-ahead log
invalidation events
source watcher
read-time version check
```

Metrics:

```text
Invalidation Latency
Missed Snoop Rate
Version Conflict Rate
Invalid Pointer Usage Rate
```

### Phase 4: Trust-Gated Promotion

Goal: prevent cache pollution.

Features:

```text
evidence logging
trust_score
pollution_score
probation cache
quorum promotion
failure-based demotion
```

Metrics:

```text
Pollution Rate
False Promotion Rate
Eviction Regret Rate
Citation Accuracy
Task Success Lift
```

### Phase 5: Production Hardening

Goal: make the system observable, recoverable, and auditable.

Features:

```text
event replay
agent recovery
metrics dashboard
alerting
audit trails
benchmark suite
```

Metrics:

```text
Health Score
p95/p99 latency
event lag
cache stability
production incident rate
```

---

## 23. Recommended Default Configuration

```yaml
l1:
  max_entries_per_agent: 50
  ttl_minutes: 60
  require_read_time_version_check: true

l2:
  max_entries_per_project: 5000
  min_trust_score: 0.45
  probation_enabled: true

coherence:
  protocol: "HC-MOESI"
  invalidation_mode: "directory_based"
  write_policy_for_core_fields: "write_invalidate"
  event_log_required: true
  lease_ttl_seconds: 300

freshness:
  require_source_version: true
  require_range_hash: true
  reanchor_on_source_change: true
  min_anchor_confidence: 0.75

promotion:
  require_strong_evidence_for_hot_set: true
  min_trust_for_l2: 0.45
  min_trust_for_hot_set: 0.70
  quorum_for_hot_set: 2

pollution_control:
  candidate_pool_enabled: true
  demote_on_repeated_open_unused: true
  demote_on_failed_task: true
  quarantine_on_contradiction: true
```

---

## 24. Engineering Checklist

### Pointer Schema

- [ ] pointer_id
- [ ] source_id
- [ ] path / anchor / line range
- [ ] source_version
- [ ] range_hash
- [ ] pointer_version
- [ ] coherence_state
- [ ] trust_score
- [ ] pollution_score
- [ ] usage evidence
- [ ] freshness metadata

### Coherence

- [ ] agent-private L1
- [ ] shared L2/L3
- [ ] holder directory
- [ ] lease manager
- [ ] WAL / event log
- [ ] invalidation event
- [ ] source change event
- [ ] event replay after agent restart
- [ ] read-time version check

### Drift Prevention

- [ ] line range hash
- [ ] heading/symbol anchor
- [ ] fuzzy re-anchor
- [ ] source watcher
- [ ] stale marking
- [ ] Invalid-state enforcement

### Cache Pollution Control

- [ ] Candidate Pool
- [ ] Probation state
- [ ] promotion gates
- [ ] trust score
- [ ] pollution score
- [ ] strong evidence logging
- [ ] failure-based demotion
- [ ] quarantine state

### Metrics

- [ ] token I/O tracking
- [ ] tool call tracking
- [ ] TTFE tracking
- [ ] stale hit tracking
- [ ] invalid pointer usage tracking
- [ ] drift tracking
- [ ] re-anchor tracking
- [ ] pollution tracking
- [ ] citation accuracy review
- [ ] agent divergence tracking
- [ ] event lag tracking

---

## 25. Key Design Conclusions

The recommended design is:

```text
Each agent maintains its own L1 to preserve locality.
Shared L2/L3 maintains durable pointer knowledge.
The Coherence Manager maintains holder directory and leases.
A pointer must acquire a lease before being modified.
All committed updates must be written to the WAL/event log.
After commit, directory-based snoop invalidation notifies old holders.
Agents mark old local pointers as I after receiving invalidation.
Source changes trigger O/I transitions through the Source Watcher.
Promotion cannot depend on one agent alone; it must pass evidence and trust gates.
```

One-sentence version:

> In multi-agent Harness Cache, use **private L1, shared L2/L3, directory-based invalidation, an HC-MOESI state machine, version checks, leased writes, and trust-gated promotion** to balance locality, coherence, drift control, and pollution resistance.
