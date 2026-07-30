# Design: Graded Memory Search (Personal / 2C)

Status: **active design** for personal Ariadne  
Audience: implementers  
Related: [../MEMORY.md](../MEMORY.md), [memory-v1.md](memory-v1.md),
[memory-scopes.md](memory-scopes.md), [prompt-assembly.md](prompt-assembly.md)

## 0. Purpose

Long chats overflow any fixed prompt window. Ariadne keeps **cheap, always-on
context** small, and offers an **explicit graded search** path when the model
needs older episodic detail.

Goals:

1. Default turn stays fast: no heavy multi-hop retrieval every message.
2. Hard recall (aliases, multi-hop, cross-session) is possible via
   `memory_search` with honest evidence.
3. Search hits are **pointers to real turns**, never LLM-invented history.
4. Personal / local-first: lexical + local embeddings first; small model only
   for deep mode.

---

## 1. Retrieval modes

Normative flow:

```text
build_context() each turn: L0 + curated + (L2 if ready) + budgeted L1
        ↓ model finds info insufficient
memory_search(scope, mode=auto|fast|deep)
        ↓
fast:  lexical + embedding (local)
auto:  fast → upgrade to deep on signals
deep:  small model: query decomp / aliases / rerank (NEVER invent history)
        ↓
hits must carry turn_id (+ session_id); no LLM-fabricated turn content
```

### 1.1 Always-on vs on-demand

| Path | When | What |
| --- | --- | --- |
| **`build_context()`** | Every turn (TurnApplication) | L0 recent raw + L3 curated (user/workspace as configured) + L2 if ready (`last_good_plus_delta`) + **budgeted** L1 (recency / light selection) |
| **`memory_search`** | Model tool call (or host API) when context is insufficient | Graded search over stored turns/summaries/index for a **named scope** |

`build_context` must **not** run deep multi-hop search by default.  
`memory_search` must **not** be a silent pre-turn classifier that always fires.

### 1.2 Mode definitions

| Mode | Mechanism | Cost | Invent history? |
| --- | --- | --- | --- |
| **fast** | Lexical (BM25 / FTS / simple token) + local embedding ANN if index present | Low, local | **No** |
| **deep** | After candidate gen: small model for **query decomposition**, **alias expansion**, and/or **rerank** of real candidates only | Higher | **No** |
| **auto** | Run **fast** first; upgrade to **deep** only when upgrade signals fire (§5) | Adaptive | **No** |

Deep mode may **rewrite the query** and **reorder hits**. It must **not**:

- synthesize turn text that was never stored
- fill gaps with “plausible” dialogue
- return hits without `turn_id` (and `session_id` when known)

If deep helpers fail (timeout, model error): return structured error or
`mode_used=fast` with `notes` — never pretend deep succeeded with empty
fabricated content.

---

## 2. Tool contract

Preferred surface: a dedicated tool (name **`memory_search`**) or a clear
action on the existing `memory` family. Catalog phrase stays short; full rules
live in `tool_schema.description`.

### 2.1 Request (JSON)

```json
{
  "query": "string — what to find",
  "scope": "session | workspace | user",
  "mode": "auto | fast | deep",
  "limit": 8,
  "before_turn_id": null
}
```

| Field | Required | Rules |
| --- | --- | --- |
| `query` | yes | Non-empty after strip; max length configurable |
| `scope` | yes | Exactly one of `session`, `workspace`, `user` — see [memory-scopes.md](memory-scopes.md) |
| `mode` | no | Default from config (`ARIADNE_MEMORY_SEARCH_MODE`, usually `auto`) |
| `limit` | no | Default 8; hard cap (e.g. 32); over cap → validation error |
| `before_turn_id` | no | Only turns **strictly before** this id (as-of search for eval / “what did we know then”) |

Host/kernel may inject active `session_id` / `user_id` from the turn; the model
does not pick another user’s store.

### 2.2 Response (JSON)

```json
{
  "mode_used": "fast | deep",
  "hits": [
    {
      "turn_id": "…",
      "session_id": "…",
      "score": 0.0,
      "snippet": "grounded excerpt from store",
      "evidence": {
        "source": "raw | summary | chunk",
        "chunk_id": "optional",
        "char_range": null
      }
    }
  ],
  "notes": "upgrade reason | demotions | index lag | empty reason"
}
```

| Field | Rules |
| --- | --- |
| `mode_used` | Actual pipeline that produced hits (`auto` is never reported as used — expand to `fast` or `deep`) |
| `hits[].turn_id` | **Required** on every hit |
| `hits[].session_id` | Required when the store is multi-session for that scope; for pure session scope, still set to active session |
| `hits[].snippet` | Substring or stored summary text **from the store** — not model prose |
| `hits[].score` | Comparable within one call; type (similarity / BM25) recorded in traces |
| `notes` | Human/debug string; empty ok |

Empty hits: `{ "mode_used": "…", "hits": [], "notes": "…" }` — success with zero
results, not a fake hit.

### 2.3 Errors (fastfail)

| Condition | Outcome |
| --- | --- |
| Unknown `scope` / `mode` | validation error |
| `limit` over hard max | validation error |
| Missing / mismatched `user_id` for user scope | isolation error ([memory-scopes.md](memory-scopes.md) §5) |
| Index backend configured but down | tool/infra error; do not return silent empty as “no memories” without `notes` |
| Deep model failure after upgrade | error **or** partial: `mode_used=fast` + `notes` explaining fallback — host documents which |

---

## 3. When to use what

Guidance for policy / tool schema / skill text (not a hidden pre-classifier):

| Situation | Path |
| --- | --- |
| Need only the last few turns | **L0** via `build_context` — no search |
| Standing prefs / durable profile | **L3 curated** in `build_context` |
| “What is true *now*?” (todos, current bindings) | **L2 only** (authoritative); do not prefer search hits over state |
| Clear keyword / error string / file name | **`memory_search` mode=fast** (or auto → stays fast) |
| Vague deixis (“that approach we tried”, “the earlier plan”) | **`memory_search`** (auto or deep) |
| Multi-hop / alias / late binding across many turns | **`memory_search` mode=deep** (or auto upgrade) |
| Cross-session episodic recall | **`memory_search`** with `scope=user` or `workspace` as appropriate — not automatic L0 |
| Recent L1 already in context is enough | Do not search again |

Authority reminder ([memory-v1.md](memory-v1.md)):

> Semantic / search hits never override Conversation State.  
> Curated durable is for standing facts, not a dump of search snippets.

---

## 4. Pipeline detail

### 4.1 Fast

```text
query
  → normalize (trim, optional unicode fold)
  → lexical search over turn text / L1 summaries (scope-filtered)
  → embedding search over L4 chunks if index ready (scope-filtered)
  → merge + dedupe by turn_id
  → optional L2-aware demotion (stale entity values)
  → top-k by score, attach snippets from store
```

If L4 is disabled or not ready: lexical-only is valid **fast**; report in
`notes` / LayerReport-style traces.

### 4.2 Deep (on top of real candidates)

Allowed small-model jobs (normative target):

1. **Query decomposition** — split multi-hop questions into sub-queries; each
   sub-query runs fast retrieval; merge candidates.
2. **Alias expansion** — pull aliases from last-good L2 / recent raw / curated;
   expand search terms (still retrieval).
3. **Rerank** — score only **existing** candidate snippets; drop low scores.

**Implementation status:**

- Default planner = **local multi-query split** (`LocalSplitPlanner`).
- Host may set `ARIADNE_MEMORY_DEEP_PLANNER=llm` to use a chat model for
  subqueries / alias_extra / rerank_order over **existing** candidate keys only
  (`make_llm_deep_planner`). Failure → `mode_used=fast` + notes.
- Single-clause local deep with no decomp: `mode_used=fast` +
  `deep:unavailable_local_noop` (must not claim deep).
- User scope uses **user episodic index** + provenance-bearing curated only.

Forbidden:

- “Recall” dialogue not present in candidates or store
- Writing new durable memory as a side effect of search
- Blocking the **next** user turn’s first token on deep search unless this call
  is inside the tool loop (tool latency is expected; `build_context` stays light)
- Reporting `mode_used=deep` when the pipeline did not change candidates beyond
  plain fast

### 4.3 Snippet grounding

Snippet sources, in order of preference for evidence honesty:

1. Raw turn excerpt (user / assistant / truncated tool outcome)
2. Ready L1 summary text for that `turn_id`
3. Indexed chunk text that maps 1:1 to stored bytes

If a turn exists but body was purged: return hit with `snippet` empty or
placeholder + `notes`, or omit hit — never invent body.

---

## 5. Auto upgrade signals

**auto** = fast first, then deep only if signals fire.

### 5.1 Upgrade when (any strong signal)

| Signal | Example |
| --- | --- |
| Fast returns **zero** hits and query is non-trivial (length / content words) | “the migration plan we outlined for billing” |
| Fast top score below threshold | Weak lexical/embedding match |
| Query looks multi-hop / compositional | “the library X that replaced Y after we dropped Z” |
| Deixis without resolvable referent in L0/L2 | “that earlier approach”, “the same bug as before” |
| Caller set high need (optional host flag) | Eval harness forces deep |

### 5.2 Do **not** upgrade when

| Signal | Why |
| --- | --- |
| Fast already returned high-confidence, diverse enough hits | Waste |
| Query is pure keyword / identifier / path | Fast is the right tool |
| Question is “what is true now” answerable by L2 | Wrong tool; prefer state |
| Deep disabled by config | `mode_used=fast`, `notes` |
| Budget / latency guard tripped | Stay on fast; honest `notes` |
| Index empty (new session, nothing to find) | Deep cannot invent past |

Must **not** be implemented as: *every turn, classify difficulty, always
pre-search*. Upgrade is **inside** `memory_search(mode=auto)` after the model
(or host) chose to search.

---

## 6. What the LLM may vs must not do

| May | Must not |
| --- | --- |
| Call `memory_search` when L0/L2/curated insufficient | Fabricate turn ids or dialogue |
| Choose `scope` and `mode` (or rely on auto) | Treat snippets as overriding L2 current state |
| Paraphrase **from** returned snippets with attribution | Claim “I remember” without a hit or L0/L2/L3 support |
| Call again with refined query | Batch unrelated side-effect tools with search as a silent memory write |
| Use `before_turn_id` when the user asks historical as-of questions | Write search results into L3 without explicit curated action |

Policy text should say: *if unsure, search; if still empty, say you don’t have
it — do not invent.*

---

## 7. Config knobs

| Knob | Env / setting | Default (suggested) | Meaning |
| --- | --- | --- | --- |
| Default mode | `ARIADNE_MEMORY_SEARCH_MODE` | `auto` | Default when tool omits `mode` |
| Fast only | mode forced `fast` | — | Disable deep entirely |
| Deep model | host model id / small local model | host-defined | Used only for decomp / aliases / rerank |
| Hit limit default / max | settings | `8` / `32` | Cap noise |
| Score threshold (auto upgrade) | settings | implementation-defined | Fast “too weak” bar |
| Embeddings | on/off + model path | off until L4 enabled | Fast path second channel |
| Lexical backend | FTS/sqlite/etc. | personal default | Fast path first channel |
| L2 demotion | on when L2 present | on | Stale hit demotion |

Traces should record: `mode_requested`, `mode_used`, upgrade reason, backend
status, hit turn ids.

---

## 8. Relation to `build_context` budgets

```text
build_context (every turn)
  L3 curated     — always high priority, small
  L2 state       — if enabled / ready
  L1 summaries   — recency + light budget (not full deep search)
  L0 recent raw  — last N turns
  (optional light L4) — only if product chooses tiny auto-recall; default off or tiny k

memory_search (on demand)
  graded pipeline → tool_result with grounded hits
```

Do not re-inject the entire search corpus into the next `build_context` unless
the host caches tool results in the normal turn transcript (standard tool
loop). That is enough for multi-step reasoning inside one turn.

---

## 9. Evaluation hooks

Cases that should pass with graded search (design-level):

1. Keyword needle in turn 30 of 80 — **fast** finds `turn_id`.
2. Alias introduced late — **deep** (or auto upgrade) + L2 aliases.
3. Multi-hop two facts in different turns — deep decomp or multi-query merge.
4. Stale value in old turn — L2 demotion; answer from state, not hit.
5. Empty store — empty hits, no fabrication.
6. `before_turn_id` — does not leak later turns.
7. Wrong `user_id` / scope isolation — error or empty limited to that scope.

Infra ERROR ≠ product FAIL ([memory-v1.md](memory-v1.md) §10).

---

## 10. Non-goals

- **2B / multi-tenant** memory control planes, shared org indices, operator
  backfill CLIs as core requirements
- **Dual durable stores** (second Mem0-like blob beside L0–L4)
- **Per-turn pre-classifier** that always scores “difficulty” and prefetches deep
  context before the model runs
- **LLM-fabricated history** as a product feature
- **Company knowledge packs** as search scope
- Query-time full-history compressor blocking `build_context` (archived path;
  use async L1 + on-demand search instead)
- Replacing L2 for “what is true now”

---

## 11. Phased delivery (search track)

| Phase | Deliverable |
| --- | --- |
| S0 | Tool contract + `scope` + lexical fast over session transcript |
| S1 | Embeddings (L4) merged into fast; demotion hooks for L2 |
| S2 | `mode=auto` upgrade signals |
| S3 | Deep: decomp + rerank; optional alias from L2 |
| S4 | Workspace/user scope indices + eval suite |

S0 is enough to stop “model invents the past”; later phases raise recall quality.

---

## 12. Decision record

| Decision | Choice | Why |
| --- | --- | --- |
| Default per-turn context | Light `build_context` | Latency + attention |
| Hard episodic recall | Explicit `memory_search` | Model/host chooses when to pay |
| Default mode | `auto` | Fast path common; deep on signal |
| Hit identity | `turn_id` + `session_id` | Auditable; no free-form memory myths |
| Deep model role | Decomp / aliases / rerank only | Never invent history |
| Scopes | session \| workspace \| user | Aligns with [memory-scopes.md](memory-scopes.md) |
| Packaging | Personal local backends first | No enterprise mesh |

---

## 13. Related

- [../MEMORY.md](../MEMORY.md) — product memory layers and APIs
- [memory-v1.md](memory-v1.md) — L0–L5 authority model
- [memory-scopes.md](memory-scopes.md) — user / workspace / session layout
- [memory-sandbox-synthesis.md](memory-sandbox-synthesis.md) — mind vs hand
- [prompt-assembly.md](prompt-assembly.md) — where memory sits in the prompt
