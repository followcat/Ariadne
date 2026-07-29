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
  See [design/memory-search.md](design/memory-search.md) (Retrieval modes).

### 3.5 Conversation state projection (advanced)

Design target inspired by memory v3 ideas:

- Append-only state changes derived from completed turns
- Deterministic reducer to a document version
- Read path requires terminal projection for enrolled sessions
- Incomplete projection → explicit not-ready error or skip with status (configurable)
- No “return partial stale state and hope”

Personal MVP may delay full projection machinery, but should not paint itself into a single-blob corner.

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

Writes must be explicit. Models should not “save memory” by free-text side channels only; prefer the `memory` tool.

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
| [design/memory-scopes.md](design/memory-scopes.md) | Personal **user / workspace / session** scopes, host layout, `user_id`, boundary with atelier `KNOWLEDGE.md` |
| [design/memory-search.md](design/memory-search.md) | **Graded retrieval** — Retrieval modes, `memory_search` contract, auto/fast/deep (hard long-context recall without per-turn pre-classifier) |

Normative product contract stays in this file; implementers follow the design notes above for scopes and search.
