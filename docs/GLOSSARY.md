# Glossary

| Term | Meaning in Ariadne |
| --- | --- |
| **Ariadne / 筑梦师** | Product name: mythic thread-bearer; Chinese “dream-builder” who keeps the path |
| **Atelier / 小作坊** | Per-account workshop: main + optional branches + 便签 |
| **便签 / KNOWLEDGE.md** | How *this* 作坊 runs (ops, paths, caveats); not Memory |
| **`/main-readonly`** | Branch sandbox mount of **live main** workspace (read-only) |
| **Kernel** | Callable application core that runs turns; not a full platform |
| **Turn** | One user invocation cycle, including internal tool loops |
| **Skill** | Procedural guidance package (`SKILL.md` + assets) |
| **Tool / Capability** | Registered callable action with schema and handler |
| **Capability registry** | Single catalog of tools; no second registry |
| **Deferred tool** | Tool known in catalog but full schema loaded on demand |
| **Tool loop** | Model ↔ tool invocation cycle inside a turn |
| **Memory layer** | One contribution to context (raw, summary, curated, semantic, state) |
| **Curated memory** | Explicit durable facts |
| **Conversation state** | Structured projection of evolving session facts (advanced) |
| **Sandbox** | Execution environment behind `SandboxPort` |
| **Host** | CLI/HTTP/app that calls the kernel |
| **Fastfail** | Clear structured failure; no silent downgrade |
| **Company Pack** | Explicitly **out of scope** enterprise extension bundle |
| **Task mode** | Closed-loop plan → act → verify → replan (`--task` / `task_mode_policy`) |
| **TaskState** | Persisted multi-step goal + steps with `done_when` checks |
| **Context Compiler** | Budgets prompt blocks and records attribution traces |
| **Connector** | Chat platform adapter; host-side, not Ariadne core |
