# Design Principles

These rules are normative for Ariadne. Implementations and PRs that violate them should be rejected unless a design doc explicitly revises the rule.

## 1. Kernel, not platform

Ariadne implements a **callable agent kernel**.

In scope:

- turn execution
- skills
- tool registry + tool loop
- memory
- sandbox **port**
- traces and structured errors

Out of scope in core:

- company packs
- chat connectors
- business system adapters
- multi-tenant control planes
- product-specific gateways (mail/egress/enterprise IAM)

External systems may embed Ariadne; they must not force their concepts into kernel types.

## 2. Fastfail is mandatory

- Unknown tool name → structured error, no invent-and-continue.
- Invalid skill pack / missing frontmatter → load fails at startup or install time.
- Undefined config → fail at construction, not mid-turn silent default soup.
- Missing memory backend dependency when required → fail clearly.
- Do **not** add fallback, compatibility shims, silent downgrade, or “best effort” paths unless a design doc explicitly requests them.

## 3. One capability registry

There is **one** logical registry of callable tools/capabilities.

- Skills do not create a second tool system.
- Sandbox CLIs are not a second registry; they are either:
  - invoked through a sandbox tool (`sandbox.exec` or equivalent), or
  - wrapped as first-class tools with explicit registration.
- Benchmarks and demos must use the same registry contract as production code.

## 4. Skills teach; tools act

| Concept | Role |
| --- | --- |
| Skill | Procedural guidance, references, templates, when-to-use |
| Tool / capability | Executable action with schema and side effects |
| Memory | Durable or session knowledge, not a substitute for tools |
| Sandbox | Execution environment for commands/files, not business protocol |

A skill may *require* tools (`requires_tools` metadata). A skill must not execute side effects by itself.

## 5. Deferred detail over eager bulk

Default orientation:

- Short **catalog** for discovery (skill index lines, capability one-liners).
- Full **skill body** loaded on demand.
- Full **tool schema** available when the model is actually choosing/calling that tool.
- Avoid stuffing all skill bodies and all tool schemas into every LLM request.

Eager mode may exist for tiny personal setups, but deferred/on-demand is the design center.

## 6. Exposure is not authorization

If Ariadne later adds policy gates:

- Showing a tool in a catalog does not mean every invocation is allowed.
- Invocation-time checks remain separate from exposure-time plans.

For personal v1, authorization may be “local user can do everything configured,” but the **API shape** should still separate:

```text
plan_exposure(...)
invoke(tool, args, context)  # re-validates
```

## 7. Memory is layered, not one blob

Do not collapse everything into a single “vector store chat history.”

Minimum conceptual layers:

1. Recent raw turns (authoritative short window)
2. Compact summaries / conversation state (mid-range structure)
3. Curated durable facts (user-approved or high-confidence durable memory)
4. Optional semantic/episodic recall (search when needed)

Write paths and read paths must be explicit. Projection lag or incomplete state must not be silently filled with stale guesses (prefer clear “not ready” / skip layer).

## 8. Tool loop is first-class

Turn execution owns:

- model exchange
- tool call extraction
- tool execution
- result packaging back to the model
- loop limits
- traces (which exchange called which tool with which args/results)

There is one application entry for a turn. Internal engine collaborators may exist, but callers should not bypass the turn application with a second ad-hoc loop.

## 9. Sandbox is a port

Sandbox is **not** Ariadne’s identity.

- Kernel depends on a narrow `SandboxPort` (create/exec/read/write/close).
- Enterprise-grade egress, mail façades, and company HTTP adapters are **not** required to implement Ariadne.
- Sandbox may be redesigned freely as long as the port and tool semantics stay honest.

## 10. Personal-first defaults

Defaults should work for a single developer machine:

- local filesystem skill store
- sqlite/files for memory MVP is acceptable if contracts stay clean
- optional docker/local process sandbox
- no mandatory remote control plane

Scaling up must not be a prerequisite for using the kernel.

## 11. Observable by default

Every turn should be able to emit:

- model call ids / usage
- tool calls and results
- skill search/load events
- memory layer contributions
- errors with stable codes

Personal open source still needs traces; without them toolcall/memory work cannot be improved honestly.

## 12. Docs lead code (bootstrap phase)

Until the public API stabilizes:

- documents in `docs/` are the source of truth
- code that contradicts docs is a bug or requires a doc update in the same change
- do not keep dead “future flexibility” branches in code or docs

## Anti-patterns

- `register(app)` plugins that mutate framework globals for company features
- Parsing platform conversation id prefixes inside kernel
- Putting secrets into skill text, tool descriptions, or model prompts
- Growing tool descriptions to compensate for bad selection
- Using memory search failures as success with empty context without marking the miss
- Second tool registry “just for demos”
