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
| Toolcall | **Duplicate `register`** raises `ARIADNE_CONFIG_INVALID` unless `replace=True`. |
| Toolcall | **Required-arg validation** before invoke → `ARIADNE_INVALID_TOOL_ARGS`. |
| Toolcall | **Redact tool `arguments`** (and failed traces) when `redact_traces` is on. |
| Memory | Default **layer_budgets** for state/curated/summary/semantic. |
| Memory | Transcript append stamps **`session_id`**; L0/delta reads can filter by session. |
| Memory | Semantic hybrid errors become layer **`failed`** (turn continues) instead of always aborting. |
| Memory | Sync build path notes **`hybrid_skipped: sync_path`**. |

## Remaining gaps (prioritized)

### Skills (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| SKILL-G05 | P1 | `auto_load` still only names; no turn-scoped body materialization |
| SKILL-G06 | P1 | `plan()` still lexical-only (hybrid used only in `search_skills`) |
| SKILL-G09 | P1 | Explicit namespace per skill root / builtin protect on manage |
| SKILL-G08+ | P2 | Configurable skill plan budgets + layer-like char reports |
| SKILL-G11–G14 | P2 | tags, targeted refs, version bump, requires_tools enforcement |

### Toolcall (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| TC-04 | P1 | Defer large real tools (e.g. `conversation_state`), not only demos |
| TC-01 | P1 | Align `ToolSpec` naming with `CapabilitySpec` / `tool_schema` in docs |
| TC-08 | P1 | Frozen toolcall correctness + schema-cost case suite |
| TC-03,12–16 | P2 | Session visibility filter, load_exact not_found, title/kind, etc. |

### Memory (remaining)

| ID | Severity | Topic |
| --- | --- | --- |
| M04 | P1 | Summaries are truncations, not async status-machine jobs |
| M09 | P1 | Semantic demotion is over-broad (entity set too large) |
| M10 | P1 | Projection claim order / lag reporting incomplete |
| M01 | P1 | Structured `MemoryContext` + `before_turn_id` |
| M03,M07,M08,M11+ | P2 | require_ready, prompt order docs, dedupe, relation caps |

## Explicit non-goals of this pass

- Full TUI / web redesign  
- Replacing FakeModel e2e with live vision tests  
- Company-pack namespaces  

## Next alignment sprint (suggested)

1. Hybrid `SkillStore.plan` + auto_load turn-scoped bodies  
2. Defer `conversation_state` / plugins via `named_deferred` + cost cases  
3. Projection ordered claim + summary status machine  
4. Tighten semantic demotion to field-level updates  
