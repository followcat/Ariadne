<p align="center">
  <img src="docs/assets/hero.jpg" alt="Ariadne — personal open-source agent kernel" width="920" />
</p>

<h1 align="center">Ariadne</h1>

<p align="center">
  <strong>Personal open-source agent kernel</strong><br/>
  Skills as the thread · Tools as the maze · Memory as the map you keep
</p>

<p align="center">
  <a href="#quick-start"><img src="https://img.shields.io/badge/quick%20start-5%20min-brightgreen?style=flat-square" alt="Quick start" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.13%2B-blue?style=flat-square" alt="Python 3.13+" /></a>
  <a href="docs/ROADMAP.md"><img src="https://img.shields.io/badge/status-v0.2%20usable-0e7-green?style=flat-square" alt="Status" /></a>
  <a href="docs/"><img src="https://img.shields.io/badge/docs-design%20%2B%20usage-111827?style=flat-square" alt="Docs" /></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-TBD-lightgrey?style=flat-square" alt="License" /></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#features">Features</a> ·
  <a href="#usage">Usage</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#documentation">Docs</a> ·
  <a href="#non-goals">Non-goals</a>
</p>

---

**Ariadne** is a callable runtime that turns one user turn into model reasoning, skill guidance, tool calls, layered memory, and optional sandboxed execution — first as a **CLI shell agent** over your project, also as a small **web UI** and a Python API.

It is **not** an enterprise multi-tenant platform, connector hub, or company packaging stack. It is the middle layer that is hard to get right and worth open-sourcing.

```text
Your prompt
  → memory context assembly
  → skill discovery / load
  → one capability registry + tool loop
  → optional sandbox (/workspace · /session)
  → persist transcript / state / result
```

## Screenshots

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/cli-demo.jpg" alt="CLI shell agent with file tools and diffs" />
      <p align="center"><sub><b>CLI</b> — one-shot <code>run</code>, streaming chat, unified diffs</sub></p>
    </td>
    <td width="50%">
      <img src="docs/assets/web-demo.jpg" alt="Web UI with BYOK provider and plugins" />
      <p align="center"><sub><b>Web</b> — register, BYOK provider, per-user plugins</sub></p>
    </td>
  </tr>
</table>

## Why Ariadne

Most “agent frameworks” give you either a thin chat wrapper, or a company platform full of connectors and deployment packs.

| You want | Ariadne gives you |
| --- | --- |
| **Callable agent** | `await agent.run(...)` / CLI turn execution |
| **Skills** | Procedural guidance on demand — not dumped into every prompt |
| **Toolcall** | **One** capability registry, deferred schemas, audited loop |
| **Memory** | Layered recall + curated facts + conversation state |
| **Sandbox** | Pluggable execution (`local` / `docker` / `null`) |
| **Host UX** | Terminal agent + optional web UI + official plugins |

## Features

- **Terminal agent** — `run` / `chat`, streaming, rich diffs, approval modes, sessions (`--continue`, `/resume`)
- **File tools** — `sandbox_read_file` / `write` / `edit` with exact-match fastfail and unified diffs
- **Sandbox** — workspace mapped to `/workspace`, scratch `/session`, toolbox profiles, observation compression
- **Memory L0–L4** — transcript, summaries, curated facts, optional semantic recall, L2 conversation state
- **Skills** — filesystem packs, hybrid search, selection plan with scores
- **Guardrails** — inbound secret redaction + injection warnings; outbound redaction
- **Official plugins** — GitLab / Redmine / Odoo as **user attributes** (CLI home store or web account)
- **Web UI** — `ariadne serve`, registration, BYOK `BASE_URL` / `API_KEY` / `MODEL`
- **OpenAI-compatible models** — any endpoint that speaks chat completions + tools

## Quick start

### 1. Install

```bash
git clone <your-fork-or-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
```

Requires **Python 3.13+**. For a no-install checkout:

```bash
export PYTHONPATH=$PWD/src
```

### 2. Configure an OpenAI-compatible LLM

Copy the example env and fill in your provider (any compatible host works — e.g. LongCat, OpenAI, local gateways):

```bash
cp .env.example .env
```

```bash
# .env — never commit this file
BASE_URL=https://api.longcat.chat/openai/v1   # or your host .../v1
API_KEY=sk-...                                # Bearer token
MODEL=LongCat-2.0                             # model id on that host
```

Ariadne also reads a workspace `.env` and process environment (`OPENAI_BASE_URL` / `OPENAI_API_KEY` aliases).

### 3. Run

```bash
# health check
ariadne doctor

# one-shot turn over the current directory (as /workspace)
ariadne run "create NOTES.md with a one-line outline of this project"

# interactive multi-turn REPL (streams by default)
ariadne chat

# web UI (register users, bind your own provider)
ariadne serve --host 127.0.0.1 --port 8420
```

Offline tests (no network):

```bash
PYTHONPATH=src python3 -m pytest -q
```

Full CLI reference: **[docs/USAGE_CLI.md](docs/USAGE_CLI.md)**.

## Usage

### CLI cheatsheet

```bash
ariadne run "…"                 # single turn
ariadne chat                    # REPL
ariadne --stream -v run "…"     # stream + tool traces
ariadne -c chat                 # continue last session
ariadne sessions                # list sessions
ariadne tools / skills / toolbox
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token glpat-…
ariadne plugin enable redmine --url … --api-key …
ariadne plugin enable odoo --url … --database … --login … --password …
# project-local override instead of ~/.ariadne/plugins.json:
ariadne plugin enable gitlab --workspace-scope --url … --token …
```

Useful flags: `--workspace`, `--session`, `--sandbox local|docker|null`, `--approval-mode auto|on-request|readonly`, `--model`, `--json`.

### Python API (shape)

```python
from ariadne.config import load_settings
from ariadne.host.compose import compose_agent

agent = compose_agent(load_settings())
result = await agent.run("Summarize this repo and open a short TODO list")
print(result.text)
```

Library-oriented constructors (`Memory.local`, `ToolRegistry`, `RunTurnCommand`, …) are documented in [docs/PUBLIC_API.md](docs/PUBLIC_API.md).

### Web UI

```bash
ariadne serve --port 8420
# open http://127.0.0.1:8420
```

Each registered user has isolated data, BYOK provider settings, and plugin credentials (`/api/me/plugins`). Playwright smoke: `scripts/verify_web.py`.

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│  Hosts:  CLI (run/chat)  ·  Web (serve)  ·  library Agent   │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  TurnApplication — memory build · skill plan · tool loop    │
│       │                                                     │
│       ├─ Memory facade (L0 transcript … L2 state …)         │
│       ├─ SkillStore (index / search / load)                 │
│       ├─ ToolRegistry (one registry, deferred exposure)     │
│       ├─ Model (OpenAI-compatible chat + tools + stream)    │
│       └─ Sandbox port (local / docker / null)               │
└─────────────────────────────────────────────────────────────┘
```

**Design pillars**

1. **One registry** for tools — never a second ad-hoc tool system  
2. **Skills ≠ tools** — skills teach; tools act  
3. **Layered memory** — raw turns, summaries, curated facts, optional semantic + state  
4. **Deferred detail** — short catalogs first; full schemas on demand  
5. **Fastfail** — invalid packs / unknown tools fail clearly; no silent downgrade  
6. **Sandbox is a port** — execution is replaceable  
7. **Personal first** — single-user friendly; no company extension model in core  

## Documentation

| Doc | Topic |
| --- | --- |
| [docs/USAGE_CLI.md](docs/USAGE_CLI.md) | CLI / web / plugins usage |
| [docs/VISION.md](docs/VISION.md) | Product vision |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Kernel architecture |
| [docs/PUBLIC_API.md](docs/PUBLIC_API.md) | Callable agent surface |
| [docs/SKILLS.md](docs/SKILLS.md) · [TOOLCALL.md](docs/TOOLCALL.md) · [MEMORY.md](docs/MEMORY.md) · [SANDBOX.md](docs/SANDBOX.md) | Subsystems |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What’s done |
| [docs/DESIGN_PRINCIPLES.md](docs/DESIGN_PRINCIPLES.md) · [NON_GOALS.md](docs/NON_GOALS.md) | Hard rules |

Reading order for design deep-dives: Vision → Principles → Architecture → Public API → Skills / Toolcall / Memory / Sandbox.

## Non-goals

Explicitly **out of scope** for core (by design):

- Company Packs / multi-company deployment models  
- First-class WeCom / Feishu / Telegram / Slack product surface  
- Multi-tenant SaaS control planes  
- Silent compatibility fallbacks  

**Official optional plugins** (GitLab / Redmine / Odoo) are supported as user-configured integrations — not as a multi-company pack system. See [docs/NON_GOALS.md](docs/NON_GOALS.md).

## Project layout

```text
src/ariadne/          kernel, memory, tools, skills, sandbox, CLI, web
skills/builtin/       example skill packs
tests/                offline pytest suite
docs/                 normative design + usage
docs/assets/          README images
scripts/              llm_smoke.py, verify_web.py
```

## Name

In myth, **Ariadne** gave Theseus a thread to escape the labyrinth.  
Here the labyrinth is multi-step tool use; the thread is skills + disciplined tool exposure; memory is what you keep so the next turn does not start blind.

## Origin note

Ariadne reinterprets ideas proven inside a private production agent core (skills runtime, deferred tool schemas, multi-layer memory). It is a **new personal open-source project**, not a fork of that platform’s company packaging, connectors, or deployment model.

## License

License to be chosen at first public release. Documentation and design may be used for discussion now.

---

<p align="center">
  <sub>Built for developers who want a real agent kernel — not another chat wrapper.</sub>
</p>
