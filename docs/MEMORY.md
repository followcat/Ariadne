# Memory

## 1. Purpose

Memory lets Ariadne stay coherent across turns without stuffing entire histories into every prompt.

Ariadne memory is **layered**. A single vector store is not enough.

## 2. Product goals

From hard-won failure modes (capacity, multi-entity state, multi-hop recall, stale updates, isolation):

1. Remember durable user facts the user cares about.
2. Track evolving conversation state (todos, decisions, bindings).
3. Recall relevant episodic details when needed.
4. Keep a short authoritative raw window.
5. Fail visibly when a required layer is incomplete — no silent amnesia disguised as success.

## 3. Layer model

```text
MemoryContext
  curated_durable        # user/global durable facts
  conversation_state     # structured mid-range state projection (optional advanced)
  turn_summaries         # compact per-turn or multi-turn summaries
  semantic_recall        # optional vector episodic snippets
  recent_raw             # last N turns, authoritative
  metadata/layers[]      # what was used, skipped, failed
```

### 3.1 Recent raw turns

- Authoritative short window
- Always preferred for the last few turns
- Includes user/assistant/tool outcomes as configured

### 3.2 Summaries

- Compress older turns
- Must not invent facts not grounded in turns
- Cacheable; invalidation on new turns

### 3.3 Curated durable memory

- Explicit entries (preferences, stable profile facts)
- Write via `memory` tool or host APIs
- Read as high-priority context
- Supports replace/forget semantics

### 3.4 Semantic / episodic recall

- Query-conditioned retrieval
- Good for long-tail details
- Known weakness if used alone for multi-hop / state transitions — pair with state/summaries
- **Cross-session episodic recall** is not automatic L0: use explicit graded
  **`memory_search`** (`scope=session|workspace|user`, `mode=auto|fast|deep`).
  Hits must carry real `turn_id` (+ `session_id`); never LLM-fabricated history.
- **Scopes (personal 2C):** session / workspace indexes under the active data
  dir; **user** scope uses a per-operator episodic index
  (`user_memory_dir/episodic/`, dual-written on turn complete) plus L3 curated
  with real provenance. Web binds `user_id` + `user_memory_dir` per account.
- **As-of:** `before_turn_id` filters episodic by chunk clock and curated by
  source-turn clock **and** entry `updated_at` (no post-cutoff curated leak).
- See [design/memory-search.md](design/memory-search.md) (Retrieval modes) and
  [design/memory-scopes.md](design/memory-scopes.md).

### 3.5 Conversation state projection (advanced)

Implemented as an opt-in advanced layer:

- Strict structured projector decisions: `apply` or `confirmed_no_change`
- Evidence quotes checked against completed-turn input/output/tool evidence
- Append-only document versions, CAS parent fencing, and per-field history
- Coordinator-locked lease validation + state apply + job completion, so a
  stale local worker cannot commit a projection after a newer claim
- Authority-aware conflict errors plus `active|superseded|expired` semantics
- Last-good-plus-raw-delta by default; strict readiness is configurable
- Disabled, pending, confirmed-no-change, failed, and succeeded remain distinct

Projection remains off by default; enabling it in the composed host wires the
configured model as a conservative structured projector.

## 4. Read API

```python
class MemoryFacade(Protocol):
    async def build_context(
        self,
        *,
        session_id: str,
        user_id: str | None,
        query: str,
        before_turn_id: str | int | None = None,
    ) -> MemoryContext:
        ...
```

TurnApplication must use this façade, not ad-hoc store reads.

`MemoryContext` should record:

- texts for prompt assembly
- layer status (`used` / `skipped` / `failed` / `disabled`)
- identifiers for traces (entry ids, version ids)

### 4.1 Typed user model

Long-lived personalization is stored separately from free-form curated notes as
typed `preference | goal | capability | constraint | relation` entries. Every
entry carries source, confidence, scope (`user | workspace | session`), status,
timestamps, revision, and superseded history. Active in-scope entries render as
the `user_model` memory layer. Authenticated host endpoints allow the user to
create, edit with revision CAS, list, and expire entries.

Typed entries also preserve temporal validity (`valid_from`, `valid_until`),
the previous value/change reason, and evidence. Automatic projection uses one
active logical key, so a preference update supersedes rather than duplicates
the old current value.

### 4.2 Memory intelligence

Completed turns are automatically inspected for explicit state changes and
high-value episode events. Deterministic extraction handles simple statements;
an optional LLM is reserved for ambiguous references and may only propose
evidence-quoted records. Explicit user statements can update typed memory;
cross-session inferences remain pending reflection candidates until confirmed.

Episode search adds problem/attempt/observation/decision/outcome chains above
the existing turn index. Deep search can traverse stored entities, relations,
timelines, decisions, and outcomes, but always returns real turn citations.
Structured prospective memories allow the host to reactivate a reminder when
workspace, query, file, tool, or event triggers match. See
[design/memory-intelligence.md](design/memory-intelligence.md).

## 5. Write API

Minimum personal v1:

```python
await memory.curated_add(session_id, content, ...)
await memory.curated_replace(...)
await memory.curated_forget(...)
await memory.index_turn(...)          # async ok
```

Advanced later:

```python
await memory.project_conversation_state(turn_id=...)
```

Durable writes must be attributable. Explicit model/tool writes use the
`memory` tool; the automatic projector may write only deterministic or
evidence-validated typed changes. Cross-session model inferences require user
confirmation.

## 6. Prompt assembly guidance

Recommended attention order (conceptual):

```text
core policy
user input
high-signal memory (curated + state)
selection results (skills)
compressed history (summaries + semantic)
recent raw
tools
```

Avoid a huge middle slab of low-signal memory that pushes tools/skills out of attention.

Token budgets per layer should be configurable and traced.

## 7. Tool surface

A single `memory` tool (or small family) for durable ops is preferred over many micro-tools.

Catalog description stays short, e.g. `durable curated memory`.  
Full calling rules live in `tool_schema.description`.

Cross-tool rules (e.g. do not batch memory writes with unrelated side effects) live in core policy when they affect multiple tools.

## 8. Evaluation discipline

Keep immutable cases with expected recalls/state, for example:

- durable cross-session preference
- forget/update correctness
- multi-entity state binding
- event sequence recall
- multi-hop / multi-seed fan-in
- stale value similarity traps
- isolation across users/sessions (even in personal software if multi-session)

Scoring rules to preserve:

- PASS/FAIL attempts, weighted score
- infrastructure ERROR ≠ product FAIL
- do not “improve” by deleting assertions or shrinking recall unfairly

## 9. Storage ports

```python
class CuratedMemoryStore(Protocol): ...
class TurnStore(Protocol): ...
class SummaryStore(Protocol): ...
class SemanticMemoryStore(Protocol): ...
class ConversationStateStore(Protocol): ...
```

Personal defaults:

- SQLite or filesystem JSONL for MVP
- optional pgvector/sqlite-vec later for semantic

Kernel does not require a hosted multi-tenant DB.

## 10. Non-goals

- Company-wide shared knowledge packs as a core concept
- Silent fallback to empty memory when a configured layer errors
- Treating tool transcripts as a substitute for curated durable memory without structure

## 11. Implementation phases

1. Session transcript + recent raw window
2. Curated memory tool + store
3. Summaries
4. Semantic recall
5. Conversation state projection (optional advanced track)

## Deep design

| Doc | Topic |
| --- | --- |
| [design/memory-v1.md](design/memory-v1.md) | Layered architecture (L0–L5), conversation state, phased delivery |
| [design/memory-scopes.md](design/memory-scopes.md) | Personal **user / workspace / session** scopes, host layout, `user_id`, user episodic root, KNOWLEDGE boundary |
| [design/memory-search.md](design/memory-search.md) | **Graded retrieval** — Retrieval modes, tool contract, as-of clocks, deep two-phase planner, config knobs |
| [design/memory-intelligence.md](design/memory-intelligence.md) | Automatic projection, episodes/causal/time memory, constrained traversal, reflection, prospective memory |
| [ROADMAP.md](ROADMAP.md) Phase 11b | Living checklist (S0–S2 done; S3/S4 partial) |
| [design/agent-closed-loop.md](design/agent-closed-loop.md) | Plan/verify loop; opt-in L2 project; Context Compiler (Phase 14) |

Normative product contract stays in this file; implementers follow the design notes above for scopes and search.
