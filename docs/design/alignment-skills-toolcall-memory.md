# Design alignment: Skills · Toolcall · Memory

Status: **active** — audit findings and landed fixes (2026-07-20).  
Related: [../SKILLS.md](../SKILLS.md), [../TOOLCALL.md](../TOOLCALL.md), [../MEMORY.md](../MEMORY.md).

## Method

Compared normative docs against `src/ariadne/skills`, `tools`, `memory`, and `kernel/turn.py`.  
This note records **gaps that remain** and **what was fixed in the alignment pass**.

## Landed in this pass

| Area | Fix |
| --- | --- |
| Skills | Compose loads skill packs with **`strict=True`** (invalid pack → composition fails). |
| Skills | `SYSTEM_POLICY` includes full **selection discipline** (prefer recommended → search → load before invent). |
| Skills | Always emit compact **`[SKILL_SELECTION]`** (auto/recommended + scores + other N); **never dump full linear index**. |
| Skills | Prompt assembly: policy → memory → skill plan → short tool catalog → runtime → recent → user. |
| Skills | **`plan_async` hybrid ranking** (lexical + embeddings); turn uses it when available. |
| Skills | **`auto_load` turn-scoped body materialization** via `[SKILL_BODY … scope=this_turn]` system blocks (cap 6k chars × 2). |
| Toolcall | **Duplicate `register`** raises `ARIADNE_CONFIG_INVALID` unless `replace=True`. |
| Toolcall | **Required-arg validation** before invoke → `ARIADNE_INVALID_TOOL_ARGS`. |
| Toolcall | **Redact tool `arguments`** (and failed traces) when `redact_traces` is on. |
| Toolcall | **`conversation_state` + `skill_manage` are `named_deferred`** (load via `tool_search`); not only demos. |
| Memory | Default **layer_budgets** for state/curated/summary/semantic. |
| Memory | Transcript append stamps **`session_id`**; L0/delta reads can filter by session. |
| Memory | Semantic hybrid errors become layer **`failed`** (turn continues) instead of always aborting. |
| Memory | Sync build path notes **`hybrid_skipped: sync_path`**. |
| Memory | **L1 summary status machine**: `enqueue` → `pending` → `process_pending` → `ready`/`not_applicable` (grounded truncate; no free invent). |
| Memory | **Projection claim** is per-session turn-order (only earliest unfinished job claimable); **`pending_lag`** reported on state layer. |
| Memory | **Semantic demotion** only for entities that already have attributes; index tags **mentioned** entities (id / alias / string attrs). |
| Memory | Structured **`MemoryContext`** Read API + **`before_turn_id`** param + **`require_ready`** (raises `ARIADNE_MEMORY_NOT_READY` on projection lag). |

## Remaining gaps (prioritized)

### Skills (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| SKILL-G09 | P1 | Explicit namespace per skill root / builtin protect on manage |
| SKILL-G08+ | P2 | Configurable skill plan budgets + layer-like char reports |
| SKILL-G11–G14 | P2 | tags, targeted refs, version bump, requires_tools enforcement |

### Toolcall (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| TC-01 | P1 | Align `ToolSpec` naming with `CapabilitySpec` / `tool_schema` in docs |
| TC-08 | P1 | Frozen toolcall correctness + schema-cost case suite |
| TC-03,12–16 | P2 | Session visibility filter, load_exact not_found, title/kind, etc. |

### Memory (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| M09+ | P2 | Field-level demotion (per attribute key / authority), not only entity-level with attrs |
| M01+ | P2 | Point-in-time `before_turn_id` applied to state/summary reads (param reserved) |
| M07,M08,M11+ | P2 | prompt order docs, dedupe, relation caps; richer summary compressor than truncate |

## Explicit non-goals of this pass

- Full TUI / web redesign  
- Replacing FakeModel e2e with live vision tests  
- Company-pack namespaces  
- Background summary/projection workers as separate processes (inline process is enough for personal v1)

## Next alignment sprint (suggested)

1. Skill namespace / builtin protect on manage (SKILL-G09)  
2. ToolSpec ↔ CapabilitySpec naming + schema-cost case suite  
3. Field-level demotion + real `before_turn_id` filtering  
4. Optional async worker loop for projection drain / summary process  
