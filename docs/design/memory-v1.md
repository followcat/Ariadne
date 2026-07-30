# Design: Memory v1 (Improved)

Status: **active design proposal** for Ariadne  
Audience: implementers  
Related: [../MEMORY.md](../MEMORY.md), [../SOURCE_MAP.md](../SOURCE_MAP.md)

## 0. Research summary (what production taught us)

From AIFlow memory evolution + memory branch benchmarks:

| Lesson | Implication for Ariadne |
| --- | --- |
| One vector store cannot hold "what is true now" | Separate **state** from **episodic recall** |
| Raw-turn single embedding misses multi-hop / late binding | Do not rely on semantic search alone for state |
| Stale similar turns resurrect dead values | Updates need authoritative overwrite path |
| Capacity overflow silently drops mid-history facts | Hard budgets + explicit missingness |
| Query-time history LLM compressors are costly/fragile | Prefer **async turn summaries**, not request-blocking helpers |
| Curated memory works for durable cross-session prefs | Keep explicit curated tool; keep it small |
| Partial projection reads are dangerous | Prefer last-good state + raw delta, or strict not-ready |
| Product FAIL vs infra ERROR must stay separate | Eval harness discipline |

Ariadne is personal open-source: keep the **invariants**, drop the **enterprise mesh** (operators, multi-tenant backfill CLIs as required core).

---

## 1. Memory taxonomy (normative)

Every piece of context answers one question:

| Layer | Question | Authority | Write path | Read path |
| --- | --- | --- | --- | --- |
| **L0 Recent raw** | What just happened? | Absolute for last N turns | turn store | always |
| **L1 Turn summary** | What happened in older turns? | Episodic, may be superseded | async after turn | retrieval + recency |
| **L2 Conversation state** | What is true *now* in this session? | Authoritative reducer | async projection ops | last-good + newer raw delta |
| **L3 Curated durable** | What should survive sessions? | Explicit user/agent durable facts | `memory` tool / host API | always (budgeted) |
| **L4 Semantic index** | Which old turns are related? | Retrieval only, never authority | embed raw/summary | optional search |
| **L5 Working scratch** | What is temporary for this turn? | Ephemeral | tool results / skill body | current turn only |

### Hard rule

> **Semantic recall never overrides Conversation State.**  
> **Curated durable never stores same-session todos/entity fields** when L2 is enabled.

Prompt markers (recommended):

```text
[CONVERSATION_STATE: AUTHORITATIVE]
[CURATED_DURABLE]
[HISTORICAL_CONTEXT: MAY BE SUPERSEDED BY CONVERSATION_STATE]
[RECENT_RAW]
[SEMANTIC_HITS: RETRIEVAL ONLY]
```

---

## 2. Target architecture

```text
                    TurnApplication
                           |
                           v
                   MemoryFacade.build_context
                           |
         +-----------------+------------------+
         |                 |                  |
         v                 v                  v
   CuratedStore      StateProjector      EpisodeStore
   (L3)              (L2)                (L0+L1+L4)
         |                 |                  |
         +--------+--------+----------+-------+
                  |
                  v
            MemoryContext
            + LayerReport[]
```

### Ports (kernel-facing)

```python
class TurnStore(Protocol):
    async def append_turn(...) -> TurnId: ...
    async def list_recent(..., limit: int) -> list[Turn]: ...
    async def get_turns(ids: list[TurnId]) -> list[Turn]: ...

class SummaryStore(Protocol):
    async def enqueue(turn_id: TurnId) -> None: ...
    async def get_ready(turn_ids: list[TurnId]) -> list[TurnSummary]: ...

class SemanticIndex(Protocol):
    async def index_turn(turn: Turn) -> None: ...
    async def search(query: str, *, limit: int) -> list[TurnHit]: ...

class CuratedStore(Protocol):
    async def snapshot(scope: Scope) -> CuratedSnapshot: ...
    async def apply(action: CuratedAction) -> CuratedResult: ...  # add/update/remove/read

class StateStore(Protocol):
    async def get_document(session_id: str) -> StateDocument | None: ...
    async def append_change(change: StateChange) -> None: ...
    async def get_last_good(session_id: str) -> StateView: ...

class StateProjector(Protocol):
    async def project_turn(turn: Turn) -> ProjectionDecision: ...
```

Personal defaults: SQLite/files for stores; optional sqlite-vec / external embed API for L4.

---

## 3. L0 Recent raw (MVP, always on)

### Policy

- Keep last `N` completed turns (default **2-4**, configurable).
- Include user text + assistant final text; tool traces summarized or truncated.
- Token-aware middle truncation with explicit markers if over budget.

### Why small N

Benchmarks show large raw windows still miss mid-range facts; raw is for *local coherence*, not long-term recall.

---

## 4. L1 Turn summary (MVP+, async)

### Contract

After turn completes:

1. Persist raw turn (authoritative).
2. Enqueue summary job (no LLM in the request critical path by default).
3. Worker produces bounded `summary_text` + optional structured fields.

### Summary properties

- Grounded in that turn only (or turn + minimal previous state snapshot).
- Must not invent facts.
- Status machine: `pending | ready | failed | not_applicable`.
- Missing summary => next turn uses raw fallback for that turn id, never pretends summary exists.

### Selection for prompt

Candidate sources:

1. recency (last K summaries)
2. semantic hits on raw/summary index (L4)
3. optional anchor expansion later

Dedupe by turn id. Cap selected summaries by token budget.

### Non-goal

No query-time "history summary LLM" blocking `build_context` (AIFlow archived this path for good reasons).

---

## 5. L2 Conversation state (design center for better memory)

This is the highest-leverage improvement over "vector chat memory."

### 5.1 Document shape (v1)

```json
{
  "schema_version": 1,
  "entities": {
    "entity_id": {
      "type": "generic|task|record|person|...",
      "aliases": ["..."],
      "attributes": {
        "field": {
          "value": "scalar-or-scalar-array",
          "source_turn_id": 12,
          "authority": "user_explicit|assistant_confirmed|inferred"
        }
      },
      "status": "active|done|cancelled|archived",
      "status_authority": "model_inferred|tool_observed|user_explicit|verified_check"
    }
  },
  "relations": {},
  "collections": {
    "todos": { "members": ["entity_id", "..."] }
  }
}
```

Hard limits (fail, do not truncate silently):

- entities / relations / collections / members caps
- rendered prompt token cap (e.g. 800-2000)

### 5.2 Allowed operations only

Closed set, e.g.:

- `ensure_entity`, `set_alias`, `set_attribute`, `set_status`
- `set_relation`, `remove_relation`
- `ensure_collection`, `collection_append|remove|move`

No free-form JSON merge from the model into the document.

### 5.3 Projection pipeline

```text
turn completed (raw committed)
  -> StateChange row pending
  -> projector LLM (strict JSON schema) OUTSIDE DB transaction
  -> decision: apply | no_change | uncertain
  -> if apply: validate evidence quotes against raw text
  -> reducer + CAS parent version
  -> append-only StateVersion
```

Rules:

1. Every op needs `evidence_quote` found in raw user/assistant text (or explicit tool result if expanded later).
2. No partial apply of a low-confidence batch.
3. `uncertain` / low confidence -> explicit `failed` or host review; retry only
   transient failures. Never auto-convert failure to no-change.
4. Processing order strictly by turn id.

For automatic cognitive projection, Assistant-authored success language is
episodic `model_assertion`, not goal authority. Setting an authoritative goal
to `done` requires a terminal event sourced from explicit user confirmation,
a closed tool observation, or Task verifier checks. Failed attempts and
unverified outcomes remain nonterminal.

### 5.4 Read semantics (choose one mode; document it)

**Recommended personal default: `last_good_plus_delta`**

```text
prompt_state = render(last_succeeded_state)
prompt_delta = render(raw_turns after watermark)  # bounded
```

- Never blocks next user turn on projection lag.
- Delta marked `[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]`.
- Delta takes precedence when conflicting.

**Optional strict mode: `require_ready`**

- If pending gap beyond threshold -> `ARIADNE_MEMORY_NOT_READY`.
- Useful for eval harnesses, not default UX.

Do **not** invent a third mode that returns partial reducer output.

### 5.5 Personal simplification vs AIFlow

Keep:

- authority split, evidence quotes, closed ops, append-only versions, last-good+delta

Drop / optionalize:

- multi-tenant operator resolve CLI as core requirement
- complex enrollment rollout matrix (still useful as feature flags)
- mandatory inspector product surface

---

## 6. L3 Curated durable memory

### Semantics

Ordered string entries with hard caps (AIFlow defaults as starting point):

- ~24 entries / scope
- ~600 chars / entry

Actions: `add | update | remove | read` (fastfail on capacity).

Scopes for personal v1:

- `user` (cross session)
- optional `session` only if L2 disabled; prefer **not** dual-writing session state into curated

### Tool design

One `memory` tool; short catalog phrase; detailed policy in schema description.

Teach the model:

- durable prefs / standing instructions -> curated
- todos / current entities / temporary decisions -> conversation state (or plain reply if L2 off)
- do not spam curated writes each turn

### Dream / background consolidation

Optional later: mine recall failures or repeated facts into curated suggestions.  
Not required for kernel MVP; if added, must create new versions, not silent overwrites.

The implemented memory-intelligence slice refines this rule: explicit user
changes may update a typed logical key automatically, while cross-session
patterns create a pending, evidence-bearing Reflection candidate. Promotion of
an inferred pattern requires an exact candidate/action/session confirmation
contract; free-text substring consent is insufficient. See
[memory-intelligence.md](memory-intelligence.md).

---

## 7. L4 Semantic index (retrieval only)

### Correct role

Return related **turn ids** (or summary ids). Never treat hit text as ground truth over L2.

### Better indexing than one blob per turn

Production failure mode: one embedding of full `User+Assistant` misses late binding and multi-seed fan-in.

Ariadne improvements:

1. **Chunk strategy**
   - chunk A: user text
   - chunk B: assistant final
   - chunk C: key tool outcomes (truncated)
   - optional chunk D: turn summary text when ready
2. **Metadata**
   - `turn_id`, `session_id`, `created_at`, `has_update_for` (entity ids if known)
3. **Query expansion (optional phase)**
   - before search, extract aliases from last-good state / recent raw
   - search with query + aliases (still retrieval, not authority)
4. **Stale-trap mitigation**
   - when hits include an entity that has a newer L2 attribute, drop or demote the hit
   - prefer hits with newer `source_turn_id` for same entity field

### Default off until needed

Personal MVP can ship L0+L3 first; enable L4 when sessions get long.

---

## 8. Prompt assembly order (memory section)

Recommended when L2 enabled:

```text
1. [CONVERSATION_STATE]
2. [CURATED_DURABLE user]
3. [HISTORICAL turn summaries + semantic hits]
4. [RECENT_RAW] including newer-than-state delta if not already covered
```

Attention notes:

- Put state + curated **above** long historical blobs.
- Keep layer budgets independent and reported in `LayerReport`.

---

## 9. Write timing relative to the turn

| Write | Timing | Blocks user reply? |
| --- | --- | --- |
| Append raw turn | on completion | no (after stream end ok) |
| Curated tool write | in tool loop | yes (tool result) |
| Turn summary | async worker | no |
| State projection | async (optional short inline gate) | default no |
| Semantic index | async | no |

**First-token latency must not wait on summary/state projection.**

Optional inline gate (few seconds): only for hosts that prefer fresher state next turn; timeout falls back to async.

---

## 10. Observability

Every `build_context` returns:

```python
@dataclass
class LayerReport:
    name: str
    status: Literal["used","skipped","failed","disabled","stale_delta"]
    token_chars: int
    item_ids: list[str]
    notes: str = ""
```

Eval harness must distinguish:

- product assertion FAIL
- infrastructure ERROR (timeouts, 5xx, missing usage)

---

## 11. Phased delivery for Ariadne

### Phase M0 - Transcript memory
- L0 recent raw only
- session transcript on disk/sqlite

### Phase M1 - Curated durable
- L3 store + `memory` tool
- caps + fastfail

### Phase M2 - Async turn summary
- L1 worker (can be in-process queue for personal)
- selection by recency

### Phase M3 - Semantic retrieval
- multi-chunk index + demotion against L2 when present

### Phase M4 - Conversation state
- L2 schema/reducer/projector
- read mode `last_good_plus_delta`

### Phase M5 - Hardening
- case suite ported from memory lab ideas (capacity, multi-entity, stale update, multi-hop)
- budgets + traces

---

## 12. Acceptance scenarios (design-level)

1. **Durable preference**: set "prefer tables over prose" -> new session still sees it (L3).
2. **Todo state**: create 5 todos, complete 2, rename 1 -> state lists exactly current open set (L2).
3. **Stale value**: set route NORTH then SOUTH -> answer SOUTH, never resurrect NORTH (L2 > L4).
4. **Late binding**: fact in turn 1, alias in turn 7, ask by alias in turn 10 -> correct value (L2 or query expansion + L1/L4).
5. **Projection lag**: kill projector mid-flight -> next turn still answers using last-good + raw delta, reports lag in LayerReport.
6. **Capacity**: curated full -> `add` returns structured capacity error, no silent drop.

---

## 13. Anti-patterns

- Single Mem0-like automatic "memory" as the only layer
- Writing every assistant sentence into curated
- Free-form model JSON merged into state without evidence
- Truncating state render without error when over hard cap
- Blocking chat on summary generation
- Treating semantic hit text as authoritative current state
- Second memory system for "tools" vs "chat"

---

## 14. Decision record

| Decision | Choice | Why |
| --- | --- | --- |
| Authority for current facts | L2 reducer | Fixes stale/vector failures |
| Default read under lag | last-good + raw delta | Personal UX unblocked; still honest |
| Summaries | async L1 | Avoid request-path helper LLM |
| Curated | explicit small store | Works for prefs; keep separate |
| Semantic | optional retrieval | Helpful, never boss |
| Personal packaging | SQLite + local worker | No company infra required |
| Automatic capture | deterministic first, LLM only on ambiguity | Better recall without a per-turn model tax |
| Episode unit | evidence-bound high-value events | Recovers a complete experience and its reasons |
| Inferred patterns | pending Reflection candidate | User controls durable learning |
| Future reminders | structured triggers in kernel; scheduling in host | Prospective memory without platform creep |
| Automatic capture consistency | per-turn stage journal + Store idempotency | Recover from partial multi-Store writes without claiming multi-file ACID |
| Episode evidence output | bounded match window + stable ids + paged expansion | Preserve causality without overflowing model context |
