# Ariadne

**Ariadne** is a personal open-source **agent kernel**: a callable runtime that turns a user turn into model reasoning, skill guidance, tool calls, memory updates, and optional sandboxed execution.

> *Skills as the thread. Tools as the maze. Memory as the map you keep.*

Ariadne is **not** an enterprise agent platform, multi-tenant SaaS core, or connector hub. It concentrates on the part that actually runs an agent:

```text
User turn
  -> Memory context assembly
  -> Skill discovery / load
  -> Tool exposure + tool loop
  -> Optional sandbox execution
  -> Persist traces / memory / result
```

## Why Ariadne

Most “agent frameworks” give you either:

- a thin chat wrapper with ad-hoc tools, or
- a company platform full of connectors, gateways, and deployment packs.

Ariadne aims at the middle layer that is hard to get right and worth open-sourcing:

| Capability | What it means |
| --- | --- |
| **Callable agent** | One clear entry: `await agent.run(...)` / turn execution API |
| **Skills** | Procedural guidance loaded on demand, not dumped into every prompt |
| **Toolcall** | One capability registry, deferred schemas, audited tool loop |
| **Memory** | Layered recall + durable curated facts + conversation state |
| **Sandbox** | Pluggable isolated execution; default simple, redesignable |

## Non-goals

Explicitly **out of scope** for Ariadne (by design):

- Company Packs / multi-company deployment models
- WeCom / Feishu / Telegram connectors as product surface
- Enterprise egress / mail / Odoo / GitLab business integrations
- Service-token multi-tenant SaaS control planes
- “God object” rewrite of a full production AIFlow stack

Those systems can *call* Ariadne later. Ariadne itself is the thread through the maze.

## Status

**v0.2 design coverage** — CLI shell agent kernel with skills, toolcall, layered memory, sandbox (local/docker/active_session), streaming, hybrid search, projection worker, and offline tests (`pytest` green).

See [docs/ROADMAP.md](docs/ROADMAP.md) for the checklist. Normative design docs remain under `docs/`.

See:

- [docs/VISION.md](docs/VISION.md) — product vision and myths
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — kernel architecture
- [docs/PUBLIC_API.md](docs/PUBLIC_API.md) — callable agent surface
- [docs/SKILLS.md](docs/SKILLS.md) — skill runtime and authoring
- [docs/TOOLCALL.md](docs/TOOLCALL.md) — capability registry and tool loop
- [docs/MEMORY.md](docs/MEMORY.md) — memory layers and contracts
- [docs/SANDBOX.md](docs/SANDBOX.md) — sandbox port (to be redesigned)
- [docs/ROADMAP.md](docs/ROADMAP.md) — phased delivery
- [docs/DESIGN_PRINCIPLES.md](docs/DESIGN_PRINCIPLES.md) — hard rules
- [docs/SOURCE_MAP.md](docs/SOURCE_MAP.md) — mapping from AIFlow branches/docs

## Intended usage (target API)

```python
from ariadne import Agent, SkillStore, Memory, ToolRegistry

agent = Agent(
    model=...,
    skills=SkillStore.from_dir("./skills"),
    memory=Memory.local("./.ariadne/memory"),
    tools=ToolRegistry.default().extend([...]),
    sandbox=None,  # or LocalSandbox() / DockerSandbox() later
)

result = await agent.run(
    "Summarize this week's notes and create a follow-up checklist",
    session_id="local-dev",
)
print(result.text)
for call in result.tool_calls:
    print(call.name, call.status)
```

This snippet is the **north-star public shape**, not a claim that the package is implemented yet.

## Design pillars

1. **One registry** for tools/capabilities — never a second ad-hoc tool system.
2. **Skills ≠ tools** — skills teach; tools act.
3. **Memory is layered** — raw turns, summaries, curated facts, optional semantic recall, optional conversation state projection.
4. **Deferred detail** — short catalogs for discovery; full schemas/bodies on demand.
5. **Fastfail** — unknown tools, invalid skill packs, missing contracts fail clearly; no silent downgrade.
6. **Sandbox is a port** — execution environment is replaceable; kernel does not own enterprise gateway topology.
7. **Personal first** — single-user / single-process friendly; no company extension model in core.

## Name

In myth, **Ariadne** gave Theseus a thread to escape the labyrinth.  
Here the labyrinth is multi-step tool use; the thread is skills + disciplined tool exposure; memory is what you keep so the next turn does not start blind.



## Tests

```bash
cd /path/to/Ariadne
PYTHONPATH=src python3 -m pytest -q
```

Offline e2e (FakeModel, no network): `tests/test_turn_e2e_fake_model.py` exercises skills, memory, sandbox, deferred tools, and the turn loop.

## CLI shell agent (basic usage)

Ariadne is meant to be used first as a **project shell agent**:

```bash
cd /path/to/your/project

# from Ariadne checkout
export PYTHONPATH=/path/to/Ariadne/src
python3 -m ariadne doctor
python3 -m ariadne run "create NOTES.md with one line about this repo"
python3 -m ariadne chat
```

Or install editable:

```bash
pip install -e /path/to/Ariadne
ariadne run "list files and summarize the project"
```

Terminal agent highlights (codex/claude-code class):

- streaming REPL with persistent history, multiline input, Ctrl+C turn cancel
- file tools with unified diffs (`sandbox_write_file` / `sandbox_edit_file`)
- `--approval-mode auto|on-request|readonly` tool approvals
- session management: `ariadne sessions`, `--continue`, `/resume`, `/model`, `/usage`
- in/out guardrails: pasted-secret redaction, injection warnings
- official plugins: `ariadne plugin enable gitlab|redmine|odoo ...`
- web UI with user registration + BYOK provider binding: `ariadne serve --port 8420`

Config: `.env` with `BASE_URL`, `API_KEY`, `MODEL` (see `.env.example`).

Details: [docs/USAGE_CLI.md](docs/USAGE_CLI.md), [docs/design/cli-shell-agent.md](docs/design/cli-shell-agent.md).

## License

TBD at first code release. Documentation may be used for design discussion now.

## Origin note

Ariadne reinterprets ideas proven inside a private production agent core (AIFlow), especially work on:

- skills runtime / selection
- toolcall evidence and deferred schemas
- multi-layer memory and conversation state

Ariadne is a **new personal open-source project**, not a fork of that platform’s company packaging, connectors, or deployment model.
