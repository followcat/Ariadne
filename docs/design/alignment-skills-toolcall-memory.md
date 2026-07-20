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
| Skills | **Explicit namespace per root** (`builtin` / `workspace` / `local` / `user`) via `from_dirs(..., namespaces=)`; **`skill_manage` refuses non-user** skills. |
| Toolcall | **Duplicate `register`** raises `ARIADNE_CONFIG_INVALID` unless `replace=True`. |
| Toolcall | **Required-arg validation** before invoke → `ARIADNE_INVALID_TOOL_ARGS`. |
| Toolcall | **Redact tool `arguments`** (and failed traces) when `redact_traces` is on. |
| Toolcall | **`conversation_state` + `skill_manage` are `named_deferred`** (load via `tool_search`); not only demos. |
| Toolcall | **`CapabilitySpec` alias** for `ToolSpec`; `catalog_phrase()` / `tool_schema()` map to docs naming. |
| Toolcall | **`schema_cost_report()`** + frozen TC-08 suite (deferred wire cost vs eager; catalog still lists deferred tools). |
| Memory | Default **layer_budgets** for state/curated/summary/semantic. |
| Memory | Transcript append stamps **`session_id`**; L0/delta reads can filter by session. |
| Memory | Semantic hybrid errors become layer **`failed`** (turn continues) instead of always aborting. |
| Memory | Sync build path notes **`hybrid_skipped: sync_path`**. |
| Memory | **L1 summary status machine**: `enqueue` → `pending` → `process_pending` → `ready`/`not_applicable` (grounded truncate; no free invent). |
| Memory | **Projection claim** is per-session turn-order (only earliest unfinished job claimable); **`pending_lag`** reported on state layer. |
| Memory | **Field-level demotion**: demote hits that tag an entity with L2 attrs but do **not** contain any current attribute value; entity-level set remains fallback. |
| Memory | **`before_turn_id` point-in-time**: filters state attrs by `source_turn_id`, summaries, semantic chunks, and L0 recent (via transcript order). |
| Memory | Structured **`MemoryContext`** Read API + **`require_ready`**. |
| Memory | **In-process `MemoryWorker`** + CLI `ariadne memory-worker` to drain summaries/projection (default projector → `no_change`). |

## Remaining gaps (prioritized)

### Skills (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| SKILL-G08+ | P2 | Configurable skill plan budgets + layer-like char reports |
| SKILL-G11–G14 | P2 | tags, targeted refs, version bump, requires_tools enforcement |

### Toolcall (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| TC-03,12–16 | P2 | Session visibility filter, load_exact not_found, title/kind completeness, etc. |

### Memory (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| M07,M08,M11+ | P2 | prompt order docs, dedupe, relation caps; richer summary compressor than truncate |
| M-worker+ | P2 | Optional out-of-process worker / LLM projector plug-in (protocol ready via `MemoryWorker.projector`) |

## Explicit non-goals of this pass

- Full TUI / web redesign  
- Replacing FakeModel e2e with live vision tests  
- Company-pack namespaces  
- Mandatory separate OS process for memory workers (in-process drain is personal v1)

## Next alignment sprint (suggested)

1. Skill plan budgets + requires_tools enforcement  
2. Tool exposure polish (session filter, not_found on load)  
3. Richer grounded summarizer + optional LLM projection plug-in  
