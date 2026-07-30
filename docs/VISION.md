# Vision

## One sentence

Ariadne is a personal open-source **agent kernel** you can call: skills guide, tools act, memory persists, sandbox optionally executes.

## Name (Ariadne · 筑梦师)

| | |
| --- | --- |
| **Myth** | **Ariadne** (Greek): offered Theseus a **thread** through the labyrinth — orientation, not the maze itself. |
| **Chinese** | **筑梦师**: craft dreams into navigable structure; leave a map worth returning to. |
| **Product metaphor** | Skills = thread · tools = maze · memory = map · sandbox / 作坊 = safe workshop. |

## Product definition

Ariadne is the runtime that owns a single turn (and multi-step tool loops inside it):

```text
input message(s)
  + session identity
  + skill store
  + tool registry
  + memory store
  + optional sandbox
  ------------------------------
  model exchanges
  tool invocations
  skill loads
  memory writes
  ------------------------------
  assistant result + traces
```

It is closer to a **library-shaped agent engine** than to a chat product or a company OS.

## Who it is for

- Individuals building local or self-hosted agents
- Developers who want skills + tools + memory without enterprise packaging
- People extracting the “good kernel ideas” from heavy production stacks into something they can own

## Who it is not for (yet)

- Multi-tenant SaaS agent platforms
- Connector marketplaces (WeCom/Feishu/Telegram as first-class products)
- Company-specific ERP/Git integration packs
- Teams needing full production egress/mail gateway topology day one

## Myth and product metaphor

| Myth | Product meaning |
| --- | --- |
| Labyrinth | Multi-step tool environment, noisy context, branching decisions |
| Thread (Ariadne’s gift) | Skills + selection discipline + short capability catalog |
| Map kept after exit | Memory layers that survive turns |
| Craftsman’s workshop | Sandbox — where hands-on work happens, redesignable |

## Success criteria

Ariadne succeeds when a developer can:

1. Install one package and run `await agent.run(...)`.
2. Drop a skill folder (`SKILL.md` + optional references) and have it discovered/loaded.
3. Register tools once and see them chosen correctly without stuffing every schema into every LLM call.
4. Persist and recall memory across turns without the model “forgetting” durable facts or inventing state.
5. Optionally run shell/file work in a sandbox backend without rewriting the kernel.
6. Read clear errors when something is undefined — never silent “best effort” behavior.

## Inspiration boundaries

Ariadne **learns from** production agent cores (skills catalogs, deferred tool exposure, memory layers, tool-loop traces).

Ariadne **does not re-home**:

- company packs / namespaces for multi-org deployments
- platform connectors as core modules
- business system adapters (Odoo, GitLab, Redmine, WeCom APIs)
- corporate confirmation/grant control planes as mandatory dependencies

Those can remain external callers or future optional plugins **outside** the kernel contract.

## Positioning vs common options

| Approach | Gap Ariadne targets |
| --- | --- |
| Raw OpenAI tool calling | No skill lifecycle, weak memory, no disciplined selection |
| LangChain-style graphs | Easy to grow into glue; skills/memory often ad-hoc |
| Full enterprise agent OS | Too heavy; company concerns dominate the kernel |
| Chat UI products | Not a callable runtime for other programs |

Ariadne’s bet: **kernel quality** (skills, toolcall, memory, sandbox port) is the durable open-source asset.

Closed-loop task mode can verify image-producing work with deterministic
`image_file` checks. The verifier reads only the workspace-bound path, validates
actual PNG/JPEG/GIF/WebP bytes (not the extension), applies optional format,
dimension, and size constraints, and records SHA-256 evidence. Visual/semantic
quality still requires the opt-in evidence-quoting semantic verifier; file
existence alone never proves visual correctness.

## Principles in one page

See [DESIGN_PRINCIPLES.md](DESIGN_PRINCIPLES.md). Architecture detail lives in [ARCHITECTURE.md](ARCHITECTURE.md).
