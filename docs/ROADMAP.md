# Roadmap

Documentation-first bootstrap is Phase 0 (this repository now).

## Phase 0 — Docs & repo skeleton (current)

- [x] Name: Ariadne
- [x] Git repository initialized
- [x] Vision, architecture, API, skills, toolcall, memory, sandbox docs
- [x] Source map from AIFlow skills/toolcall/memory work
- [ ] License selection
- [ ] Package skeleton (`pyproject.toml`) — next after docs review

## Phase 1 — Callable turn MVP

Deliver a real `await agent.run(...)` with:

- OpenAI-compatible model adapter (or Responses adapter)
- eager tool registry + tool loop + traces
- session transcript persistence (filesystem/sqlite)
- recent raw memory window only
- filesystem skill store with index injection (no search yet)
- `NullSandbox`

Acceptance:

- CLI or python demo completes multi-step tool call on a toy tool
- unknown tool fails with stable error code

## Phase 2 — Skills runtime

- `search_skills` lexical
- `load_skill` turn-scoped body
- skill validation CLI
- selection discipline in policy
- example builtin skills (authoring notes, generic runbook)

Acceptance:

- skill load improves a scripted multi-step task vs no-skill baseline

## Phase 3 — Toolcall efficiency

- deferred tool exposure + `tool_search`
- catalog vs schema layering enforced in builtins
- schema size metrics in traces
- initial case suite (functional + cost)

Acceptance:

- deferred mode reduces upfront schema chars without losing tool coverage cases

## Phase 4 — Memory depth

- curated memory tool + store
- summaries
- optional semantic recall
- layer budgets + metadata

Acceptance:

- durable preference survives new session
- at least one multi-turn state case passes

## Phase 5 — Sandbox redesign v1

- `LocalWorkdirSandbox`
- `sandbox.exec`
- output truncation markers
- optional Docker backend experimental

Acceptance:

- model can create a file via sandbox and read back content in-loop

## Phase 6 — Advanced memory (optional track)

- conversation state projection
- stricter not-ready semantics
- evaluation suite expanded from memory lab ideas

## Phase 7 — Polish for public 0.1

- docs sync with code
- typed public exports
- CI tests
- example project
- performance notes

## Explicitly deferred forever (unless product changes)

- Company Pack system
- WeCom/Feishu/Telegram connectors in core
- Odoo/GitLab/Redmine adapters in core
- enterprise mail/egress gateway mesh as required runtime

## Suggested contribution order

1. Agree docs
2. Package skeleton + TurnApplication empty loop
3. Model adapter + traces
4. Tools
5. Skills
6. Memory
7. Sandbox

Do not start with company packaging or connector work.
