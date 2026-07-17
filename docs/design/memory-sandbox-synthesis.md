# Design: Memory x Sandbox Synthesis

Status: active  
Related: [memory-v1.md](memory-v1.md), [sandbox-v1.md](sandbox-v1.md)

## Why these two together

A callable agent fails in two classic ways:

1. **Mind failure** — forgets, resurrects stale facts, confuses history with current state.
2. **Hand failure** — cannot safely run commands, manage files, or return huge outputs.

Ariadne treats them as separate subsystems with a thin joint:

```text
Memory answers: what is true / what happened / what to recall
Sandbox answers: how to act on a computer
```

They meet only at:

- tool loop traces (sandbox results may be summarized into turn raw / L1)
- optional state attributes derived from tool evidence (L2 projector may quote tool outputs later)
- workspace paths referenced in memory as plain strings (not mounts into memory DB)

## Joint rules

1. Sandbox outputs are **evidence**, not automatic durable memory.
2. Do not auto-write full shell logs into curated memory.
3. Turn summary may mention commands/results at high level.
4. Conversation state may store *user-relevant outcomes* (e.g. "report path=/workspace/out.csv") only via projection ops with evidence.
5. Memory never executes; sandbox never decides policy beyond backend config.

## Recommended MVP stack for Ariadne

```text
TurnApplication
  Memory: L0 raw + L3 curated
  Tools: memory + sandbox.exec + search/load skills
  Sandbox: LocalWorkdir per_turn
```

Then grow:

```text
+ L1 async summaries
+ L4 semantic retrieval
+ L2 conversation state
+ Docker sandbox / active_session
```

## Evaluation matrix (personal)

| Scenario | Memory layer | Sandbox |
| --- | --- | --- |
| Remember user style prefs | L3 | none |
| Multi-step coding in one turn | L0 | exec + /session |
| Multi-turn coding project | L2 paths + L0 | active_session or /workspace |
| "What did we decide about X?" | L2 | none |
| "Find that error from yesterday" | L1/L4 | none |
| Generate CSV and summarize | L0 + maybe L2 path | exec |

## Implementation order (pragmatic)

1. Transcript + tool loop + local sandbox
2. Curated memory tool
3. Skills search/load
4. Summaries + semantic
5. Conversation state
6. Stronger sandbox backends

This matches a personal open-source path: useful early, correct later.
