# Design Note: Prompt Assembly

Status: active  
Related: [../SKILLS.md](../SKILLS.md), [../MEMORY.md](../MEMORY.md), [../TOOLCALL.md](../TOOLCALL.md)

## Goals

- Keep high-attention regions for user goal + actionable plans
- Avoid middle-of-prompt swamps
- Separate catalog short text from full schemas/bodies

## Recommended block order (personal v1 — matches `TurnApplication`)

```text
1. Core policy (incl. skill selection discipline + cross-tool rules)
2. High-signal memory system block
   (working set → pinned user model → retrieved profile →
   query-selected summaries → semantic; layer budgets traced).
   Low-information acknowledgements skip retrieved profile, summaries, and
   semantic. Immediate deixis (“刚才” / previous reply) skips semantic and
   non-recent summaries. Working set, pinned preferences/constraints/goals,
   state delta, recent raw, reflection, and prospective stay.
3. Skill selection plan [SKILL_SELECTION] (auto_load / recommended / other + budget line)
4. Turn-scoped auto_load skill bodies [SKILL_BODY … scope=this_turn] (budgeted)
5. Short tool catalog (discovery only; deferred marked)
6. Runtime context (time, session_id, turn_id)
7. Recent raw turns (L0)
8. User input (strong attention region — last before model reply)
```

Wire schemas (eager tools + loaded deferred) are sent via the model tools
parameter, not pasted into the prompt.

## Budgets

Budgets are configuration, not vibes. Each layer should report used tokens/chars in traces.

| Layer | Config |
| --- | --- |
| Memory layers | `MemoryFacade.layer_budgets` |
| Skill plan text | `SkillPlanBudgets.plan_chars` |
| Auto-load bodies | `SkillPlanBudgets.auto_body_max` × `auto_body_chars` |
| Tool schemas | deferred exposure + `schema_cost_report()` |

When over budget:

- drop lowest-signal layers first
- never drop user input
- never silently drop policy safety rules
- mark truncation explicitly (`[ariadne: … truncated …]`)

## Anti-patterns

- Full skill bodies for all skills every turn
- Full tool schemas for all tools every continuation
- Duplicating the same long rule in catalog + schema + policy
- Silent omission of memory layers when projection lags (use status / require_ready)
