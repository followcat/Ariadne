# Design alignment: Skills · Toolcall · Memory

Status: **active** — audit findings and landed fixes (2026-07-20).  
Related: [../SKILLS.md](../SKILLS.md), [../TOOLCALL.md](../TOOLCALL.md), [../MEMORY.md](../MEMORY.md).

## Method

Compared normative docs against `src/ariadne/skills`, `tools`, `memory`, and `kernel/turn.py`.  
This note records **gaps that remain** and **what was fixed in the alignment pass**.

## Landed (summary)

Earlier P0/P1/P2 landings are preserved. Latest **P2 close-out**:

| Area | Fix |
| --- | --- |
| Skills G08 | **`SkillPlanBudgets`** (auto/recommended limits, body max/chars, plan_chars); plan **`report`** + `format_plan_text` budget line |
| Skills G11–14 | **tags** in frontmatter; **targeted `references=`** on load_skill; **version bump** on manage update; **requires_tools** missing report + auto_load skip |
| Toolcall TC-03+ | **`session_visible`** filter on exposure/catalog; **`load_exact_report` not_found**; **title/kind** filled via `ensure_titles` |
| Memory M07+ | **prompt-assembly.md** matches turn order; **grounded_compress** summarizer; **relation/collection caps** + edge dedupe |
| Memory worker | **`make_projector`** plug-in hook for sync/async LLM projectors |

Full historical landed table lives in git history of this file; focus remaining gaps below.

## Remaining gaps (post P2)

| ID | Severity | Topic |
| --- | --- | --- |
| SKILL+ | P3 | Configurable host settings for SkillPlanBudgets via CLI flags |
| TC+ | P3 | Provider-native deferred search mode |
| M+ | P3 | Out-of-process worker binary; LLM summarizer (beyond grounded extract) |

## Explicit non-goals

- Full TUI / web redesign  
- Company-pack namespaces  
- Mandatory separate OS process for memory workers  

## Related notes

- [prompt-assembly.md](prompt-assembly.md) — block order + budgets  
- [memory-v1.md](memory-v1.md) — layer semantics  
