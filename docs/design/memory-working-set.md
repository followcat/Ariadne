# Design: Conversation State Working Set

Status: **active design** for Ariadne  
Audience: implementers  
Related: [../MEMORY.md](../MEMORY.md), [memory-v1.md](memory-v1.md),
[prompt-assembly.md](prompt-assembly.md)

## 0. Problem

L2 conversation state is the authoritative answer to “what is true *now* in
this session.” The first implementation stored one JSON document per session
and rendered the whole document into every turn.

That couples two different capacities:

- how much durable state a long personal session may accumulate
- how many characters a single model prompt may spend on current state

A growing todo list, entity map, or project roster then either blows the
prompt budget or fails the whole layer. Truncating the document silently
would lie. Blocking the next user turn on a full render is the wrong default
for a personal assistant.

## 1. Separation of authority and view

```text
accepted operations
        |
        v
append-only event stream --replay--> complete typed projection
                                           |
                              query-scoped selection
                                           |
                                           v
                          bounded working set (soft / hard char caps)
                                           |
                    omitted current facts: conversation_state_lookup
```

| Artifact | Question | May grow with activity? |
| --- | --- | --- |
| Event stream | What mutations were accepted? | Yes (source of truth) |
| Typed projection | What is the complete current state? | Yes |
| Working set | What current facts enter *this* prompt? | No — bounded every turn |

Hard rules:

1. The working set is a **non-exhaustive view**. It is never a second source
   of truth.
2. A lookup miss does **not** mean the fact is absent. It means it was not
   on this page.
3. Semantic / summary / curated hits never override a current projection
   field.
4. There is still only one read-mode pair: default `last_good_plus_delta`,
   optional `require_ready`. Do not invent a third mode that returns a
   half-reduced document.
5. Overflow of the working-set **hard** budget is
   `ARIADNE_MEMORY_WORKING_SET_OVERFLOW`. Never silent truncate.

## 2. Storage (personal)

L2 persists in a stdlib SQLite file beside the memory root. Other memory
stores (transcript, curated, episodes, reflection, prospective, user model)
stay on their existing JSON files.

Logical path `state.json` remains the **identity path** for capture-journal
affinity (`store_identity`). The durable file is a sibling `.sqlite`
database. On first open, if `state.json` still holds documents and the
database is empty, the store imports each session as `seq=1`
`import_snapshot` plus a full projection, then renames the JSON file to
`state.json.migrated`. Runtime never dual-reads. If both the live JSON and a
non-empty database exist, open **fastfails**.

Tables (normative names):

- `state_documents` — session watermark, version, event seq, projection hash
- `state_events` — `(session_id, seq, op_index)` accepted operations
- `state_versions` — CAS parents and closed operation batches for as-of replay
- `state_projection_items` — current `entity` / `fact` / `relation` /
  `collection` rows
- `state_collection_members` — ordered members; never serialized into one
  projection blob
- `state_idempotency` — per-session apply keys

Reducer, closed op set, evidence quotes, authority lattice, Host-only
`task_goal_bindings`, and `render_model_safe` stay in `state.py`. SQLite is
the persistence port, not a second reducer.

## 3. Capacity (two contracts)

Storage caps fail the write. Prompt caps fail the assembler. They are not
the same number.

| Contract | Default |
| --- | --- |
| Entities | 4_096 |
| Relation types / edges per type | 64 / 256 |
| Collections | 128 |
| Members per collection | 4_096 |
| Working-set soft / hard chars | 6_000 / 8_000 |
| Complete-mode gate | `projection_rows ≤ 50` and full render ≤ soft |
| Lookup page | `limit ≤ 32`, response ≤ 16_000 bytes |

`MemoryLimits` exposes `working_set_soft_chars`, `working_set_hard_chars`,
and `lookup_page_limit`. Profiles may scale the char budgets; storage caps
stay safety ceilings.

## 4. Working-set assembler

Input: last-good projection (or as-of projection), current `query`.

1. If there are no rows → empty layer (`skipped`).
2. If `rows ≤ 50` **and** a complete render fits the soft budget →
   `selection_mode=complete`. Header states the view is the full current
   projection.
3. Otherwise `selection_mode=selected`: exact ref hits, then lexical
   overlap (unicode tokens + CJK bigrams). Embedding search is reserved
   for a later increment (lexical zero-result only, and only if a
   provider is already configured). Close over entities for selected
   facts/relations.
4. Collection rows render `member_count` and an instruction to look up.
   They never render `members=[]` when the collection has members.
5. If the assembled text exceeds the hard cap →
   `ARIADNE_MEMORY_WORKING_SET_OVERFLOW`.

Marker:

```text
[CONVERSATION_STATE_WORKING_SET]
```

`last_good_plus_delta` still appends newer raw turns as
`[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]`. Delta wins on conflict.
Projection lag does not block the user turn.

## 5. Lookup tool

One registry entry: `conversation_state_lookup`.

Request: `query` (required), `limit`, `cursor` (empty on first page).

Resolution order:

1. Exact `(session_id, item_ref)` or collection name
2. SQLite FTS / LIKE lexical
3. Optional embedding search (same rule as the assembler)

Response: items, `has_more`, `next_cursor`, `projection_seq`,
`semantic_status`. Cursor binds session, projection seq, collection
position, and query hash. A newer projection returns
`ARIADNE_MEMORY_STATE_CURSOR_STALE`. An item that cannot fit one page
returns `ARIADNE_MEMORY_LOOKUP_ITEM_TOO_LARGE`.

Lookup returns current rows only (`active` facts; not `superseded` /
`expired`). An empty page is an empty page, not a negative assertion.

`conversation_state` `action=read` returns the same working-set snapshot
the facade would inject (plus `omitted_count` / `selection_mode`). It does
not dump collection members.

## 6. Prompt selection (non-state layers)

Working set and pinned typed personalization are high-signal. They are not
evicted by historical layers.

| Query class | Skip | Keep |
| --- | --- | --- |
| Low-information ack (`好的` / `继续` / `收到` / `ok` / `thanks`, closed set) | retrieved profile, summaries, semantic | working set, pinned user model, state delta, recent raw, reflection, prospective |
| Immediate deixis (`刚才` / previous reply) | semantic and non-recent summaries | working set, pinned, delta, recent raw |
| Informative | query-select profile + up to four summaries | as above |

Pinned user-model types: active `preference`, `constraint`, `goal`, and
`capability` in scope. User-scope curated notes stay always-on (they are
the durable `memory` tool). Workspace curated and remaining typed entries
are query-selected.
Other typed entries and curated notes are retrieved together under
`[RETRIEVED_PROFILE]`. Exact NFKC / case-insensitive duplicates of a pinned
or typed value are omitted from the curated view only (data is unchanged).

Recent-raw turn ids must not also occupy a summary slot.

## 7. Observability

`LayerReport` for `conversation_state` records `selection_mode`,
`omitted_count`, `projection_seq`, and char counts. Host inspection
(`MemoryContext`, CLI `/memory read`) may surface the same fields. There is
no separate operator control plane.

## 8. Non-goals

- Merging Episode events into the L2 stream
- Requiring an embedding call to assemble every working set
- Treating lookup miss as “this fact does not exist”
- Auto-promoting inferred reflection candidates into curated memory
- Multi-device sync, conversation fork as a kernel primitive, or a hosted
  enrollment / cutover product
