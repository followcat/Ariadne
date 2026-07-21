# Design alignment: Skills · Toolcall · Memory

Status: **active** — audit findings and landed fixes (2026-07-20).  
Related: [../SKILLS.md](../SKILLS.md), [../TOOLCALL.md](../TOOLCALL.md), [../MEMORY.md](../MEMORY.md).

## Method

Compared normative docs against `src/ariadne/skills`, `tools`, `memory`, and `kernel/turn.py`.  
This note records **gaps that remain** and **what was fixed**.

## Landed (including P3)

| Area | Fix |
| --- | --- |
| Skills | Strict load, hybrid `plan_async`, auto_load bodies, namespaces, builtin protect |
| Skills P3 | **Host `SkillPlanBudgets`** via Settings / env / CLI (`--skill-auto-load`, …) |
| Toolcall | Deferred large tools, CapabilitySpec alias, schema-cost report, visibility, not_found |
| Toolcall P3 | **`client_search_mode`**: `function` \| `native` (auto-materialize) \| `none` |
| Memory | Status machine, ordered claim, field demotion, before_turn_id, MemoryContext |
| Memory P3 | **`summary_mode=grounded|llm`**, **`spawn_worker_process`** + `python -m ariadne.memory.worker_main` |

## Configuration (P3 host)

| Setting | Env | CLI |
| --- | --- | --- |
| skill auto_load limit | `ARIADNE_SKILL_AUTO_LOAD_LIMIT` | `--skill-auto-load` |
| skill recommended limit | `ARIADNE_SKILL_RECOMMENDED_LIMIT` | `--skill-recommended` |
| auto body max / chars | `ARIADNE_SKILL_AUTO_BODY_MAX` / `_CHARS` | `--skill-body-max` / `--skill-body-chars` |
| plan chars | `ARIADNE_SKILL_PLAN_CHARS` | `--skill-plan-chars` |
| tool search mode | `ARIADNE_TOOL_SEARCH_MODE` | `--tool-search-mode` |
| summary mode | `ARIADNE_SUMMARY_MODE` | `--summary-mode` |

### tool_search_mode

- **function** (default): offer `tool_search`; deferred schemas off-wire until load
- **native**: no `tool_search` on wire; first invoke of a deferred name auto-materializes
- **none**: deferred tools not loadable (eager-only wire)

### Out-of-process memory worker

```bash
ariadne memory-worker --once
ariadne memory-worker --subprocess --once   # separate OS process
python -m ariadne.memory.worker_main --data-dir ./.ariadne --once
```

Shared JSON (`summaries.json`, `projection_jobs.json`, `state.json`) uses
**fcntl sidecar locks** + atomic replace (`*.lock` / `*.tmp`). Agent turns and
sub-process workers **may run concurrently** without lost updates. Summary
compression (including LLM) runs **outside** the lock after a short claim.

### LLM summary mode

`ARIADNE_SUMMARY_MODE=llm` / `--summary-mode llm` installs `make_llm_compressor`.
The compressor runs the model via a **thread-bound event loop** when called from
an already-running asyncio loop (turn end / worker), so it no longer silent-
falls-back to grounded solely due to nested `asyncio.run`.

## Landed (Phase 11 personal completeness)

| Area | Fix |
| --- | --- |
| Skills | `load_skill(section=…)` body slice; optional `trigger_clues` / `distinct_from` / `key_difference` |
| Memory | `memory/consolidation.py` + `memory-worker --consolidate [--apply]` → L3 curated |
| Host approval | `cli/grants.py` durable pending→approved→executed/expired for on-request |

## Remaining (optional later)

| Topic | Notes |
| --- | --- |
| Provider-native API field | Wire deferred catalog into vendor-specific “tools search” fields when a provider documents them |
| LLM projector default | Hosts plug in via `make_projector`; no cloud dependency in core |
| Skill budget CLI UX | Already flags; TUI knobs optional |
| MCP / OTel | Optional host adapters — not core dependencies |

## Explicit non-goals

- Company-pack namespaces  
- Mandatory cloud LLM for summaries (llm mode always falls back to grounded)  

## Related notes

- [prompt-assembly.md](prompt-assembly.md)  
- [memory-v1.md](memory-v1.md)  
