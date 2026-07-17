# Glossary

| Term | Meaning in Ariadne |
| --- | --- |
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
| **Connector** | Chat platform adapter; host-side, not Ariadne core |
