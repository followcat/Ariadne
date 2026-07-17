# Design Note: Prompt Assembly

Status: active  
Related: [../SKILLS.md](../SKILLS.md), [../MEMORY.md](../MEMORY.md), [../TOOLCALL.md](../TOOLCALL.md)

## Goals

- Keep high-attention regions for user goal + actionable plans
- Avoid middle-of-prompt swamps
- Separate catalog short text from full schemas/bodies

## Recommended block order

```text
1. Core policy (incl. skill selection discipline + cross-tool rules)
2. User input
3. High-signal memory (curated + conversation state)
4. Skill selection plan (auto_load / recommended / other)
5. Compressed history (summaries + semantic hits)
6. Recent raw turns
7. Runtime context (time, session ids)
8. Tools (eager schemas + search); deferred schemas off-wire
```

## Budgets

Budgets are configuration, not vibes. Each layer should report used tokens/chars in traces.

When over budget:

- drop lowest-signal layers first
- never drop user input
- never silently drop policy safety rules

## Anti-patterns

- Full skill bodies for all skills every turn
- Full tool schemas for all tools every continuation
- Duplicating the same long rule in catalog + schema + policy
