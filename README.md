<p align="center">
  <img src="docs/assets/hero.jpg" alt="Ariadne — personal open-source agent kernel" width="920" />
</p>

<h1 align="center">Ariadne</h1>

<p align="center">
  <strong>Personal open-source agent kernel</strong><br/>
  Skills as the thread · Tools as the maze · Memory as the map you keep
</p>

<p align="center">
  <strong>English</strong> ·
  <a href="README.zh-CN.md">简体中文</a>
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
  <a href="#hosts--ui">Hosts &amp; UI</a> ·
  <a href="#usage">Usage</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#documentation">Docs</a>
</p>

---

**Ariadne** is a callable runtime that turns one user turn into model reasoning, skill guidance, tool calls, layered memory, and optional sandboxed execution.

Default human interface: a **CLI shell agent** over your project workspace (bare `ariadne` → REPL, codex-style).  
Optional: a **Grok-style web UI** (`ariadne serve`) with sidebar history, light/dark themes, session titles, and image paste.

It is **not** an enterprise multi-tenant platform, connector hub, or company packaging stack.

```text
Your prompt
  → memory context assembly
  → skill discovery / load
  → one capability registry + tool loop
  → optional sandbox (/workspace · /session)
  → persist transcript / state / result
```

## Screenshots

<p align="center">
  <img src="docs/assets/web-demo.jpg" alt="Ariadne web — dark theme, sidebar sessions, chat" width="920" />
</p>
<p align="center"><sub><b>Web (dark)</b> — left history + new chat · session topic titles · markdown stream · bottom composer · Provider / plugins / theme</sub></p>

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/web-demo-light.jpg" alt="Ariadne web light theme" />
      <p align="center"><sub><b>Web (light)</b> — same shell, light theme toggle (☀/☾)</sub></p>
    </td>
    <td width="50%">
      <img src="docs/assets/cli-demo.jpg" alt="Ariadne CLI REPL with tools and diffs" />
      <p align="center"><sub><b>CLI</b> — bare <code>ariadne</code> REPL · tools · diffs · <code>/title</code> · <code>/image</code></sub></p>
    </td>
  </tr>
</table>

## Why Ariadne

Most “agent frameworks” give you either a thin chat wrapper, or a company platform full of connectors and deployment packs.

| You want | Ariadne gives you |
| --- | --- |
| **Callable agent** | `await agent.run(...)` / CLI turn execution |
| **Skills** | On-demand procedural guidance + compact selection plan |
| **Toolcall** | **One** capability registry, deferred schemas, audited loop |
| **Memory** | Layered recall + curated facts + conversation state |
| **Sandbox** | Pluggable execution (`local` / `docker` / `null`) |
| **Host UX** | Terminal agent + Grok-style web UI + official plugins |

## Features

- **CLI first** — bare `ariadne` enters interactive REPL; `run` / `exec` one-shot; streaming, rich diffs, approvals
- **Sessions** — continue / resume; **topic titles** (auto summary + manual `/title` or web rename)
- **Images** — CLI `/image` (path or clipboard); web paste / drag-drop; fails clearly if model is not multimodal (`ARIADNE_VISION`)
- **File tools** — `sandbox_read_file` / `write` / `edit` with unified diffs
- **Memory L0–L4** — transcript, summaries, curated facts, semantic recall, L2 conversation state
- **Skills** — packs, hybrid search, scored selection plan (no full-index dump)
- **Guardrails** — secret redaction in/out; injection warnings
- **Official plugins** — GitLab / Redmine / Odoo as **user attributes** (home store or web account; secrets shown as `***`)
- **Web UI** — sidebar history, BYOK provider modal, plugins modal, light/dark theme
- **OpenAI-compatible models** — chat completions + tools

## Hosts & UI

### CLI shell (default)

```bash
ariadne                 # interactive REPL (active_session, stream on)
ariadne "do the thing"  # REPL + first user turn
ariadne run "…"         # non-interactive one-shot
ariadne exec "…"        # alias of run
```

Useful in-REPL commands: `/help`, `/title`, `/image`, `/resume`, `/status`, `/mode`, `/exit`.

### Web UI

```bash
ariadne serve --host 127.0.0.1 --port 8420
# → http://127.0.0.1:8420
```

| Area | Behavior (matches current UI) |
| --- | --- |
| **Sidebar** | New chat · history with **topic titles** · Provider / plugins / appearance / logout |
| **Main** | Session title in top bar · model chip · markdown streaming · tool pills |
| **Composer** | Bottom floating input · Enter send · paste/drop images |
| **Theme** | Light / dark toggle (localStorage + system default) |
| **Sessions** | Switch reloads history; double-click row or click title to rename; auto topic summary |

## Quick start

### 1. Install

```bash
git clone <your-fork-or-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
```

Requires **Python 3.13+**. Checkout without install:

```bash
export PYTHONPATH=$PWD/src
```

### 2. Configure an OpenAI-compatible LLM

```bash
cp .env.example .env
```

```bash
# .env — never commit this file
BASE_URL=https://api.longcat.chat/openai/v1
API_KEY=sk-...
MODEL=LongCat-2.0
# optional: ARIADNE_VISION=auto|on|off
```

### 3. Run

```bash
ariadne doctor
ariadne
ariadne serve --port 8420
```

```bash
PYTHONPATH=src python3 -m pytest -q
```

Full host guide: **[docs/USAGE_CLI.md](docs/USAGE_CLI.md)** · **[docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md)**.

## Usage

### CLI cheatsheet

```bash
ariadne
ariadne "create NOTES.md with a one-line outline"
ariadne run "summarize README.md"
ariadne -c                      # continue last session
ariadne resume --last
ariadne sessions
ariadne plugins
ariadne plugin enable gitlab --url … --token …
# in REPL:
#   /title 部署脚本     /title --refresh
#   /image ./shot.png   /image          # clipboard
#   /help /status /exit
```

### Python API (shape)

```python
from ariadne.config import load_settings
from ariadne.host.compose import compose_agent

agent = compose_agent(load_settings())
result = await agent.run("Summarize this repo and open a short TODO list")
print(result.text)
```

See [docs/PUBLIC_API.md](docs/PUBLIC_API.md).

## Architecture

```text
┌─────────────────────────────────────────────────────────────┐
│  Hosts:  CLI (default REPL)  ·  Web (serve)  ·  library     │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  TurnApplication — memory · skill plan · tool loop          │
│       ├─ Memory facade (L0…L2… semantic)                    │
│       ├─ SkillStore (compact SKILL_SELECTION + search/load) │
│       ├─ ToolRegistry (one registry, deferred exposure)     │
│       ├─ Model (OpenAI-compatible + optional vision)        │
│       └─ Sandbox port (local / docker / null)               │
└─────────────────────────────────────────────────────────────┘
```

**Design pillars:** one registry · skills ≠ tools · layered memory · deferred detail · fastfail · sandbox as a port · personal first.

## Documentation

| Doc | Topic |
| --- | --- |
| [README.zh-CN.md](README.zh-CN.md) | 中文介绍 |
| [docs/USAGE_CLI.md](docs/USAGE_CLI.md) · [docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md) | Host usage |
| [docs/design/alignment-skills-toolcall-memory.md](docs/design/alignment-skills-toolcall-memory.md) | Design alignment notes |
| [docs/VISION.md](docs/VISION.md) · [ARCHITECTURE.md](docs/ARCHITECTURE.md) · [PUBLIC_API.md](docs/PUBLIC_API.md) | Design core |
| [docs/SKILLS.md](docs/SKILLS.md) · [TOOLCALL.md](docs/TOOLCALL.md) · [MEMORY.md](docs/MEMORY.md) · [SANDBOX.md](docs/SANDBOX.md) | Subsystems |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Delivery checklist |

## Non-goals

- Company Packs / multi-company deployment models  
- First-class WeCom / Feishu / Telegram / Slack product surface  
- Multi-tenant SaaS control planes  
- Silent compatibility fallbacks  

Official optional plugins (GitLab / Redmine / Odoo) are user-configured integrations — not multi-company packs. See [docs/NON_GOALS.md](docs/NON_GOALS.md).

## Project layout

```text
src/ariadne/          kernel, memory, tools, skills, sandbox, CLI, web
skills/builtin/       example skill packs
tests/                offline pytest suite
docs/                 design (normative EN) + usage
docs/zh/              Chinese user docs
docs/assets/          README images (hero, CLI, web dark/light)
scripts/              llm_smoke.py, verify_web.py
```

## Name

In myth, **Ariadne** gave Theseus a thread to escape the labyrinth.  
Here the labyrinth is multi-step tool use; the thread is skills + disciplined tool exposure; memory is what you keep so the next turn does not start blind.

## Origin note

Ariadne reinterprets ideas proven inside a private production agent core (skills runtime, deferred tool schemas, multi-layer memory). It is a **new personal open-source project**, not a fork of that platform’s company packaging or connectors.

## License

License to be chosen at first public release. Documentation and design may be used for discussion now.

---

<p align="center">
  <sub>Built for developers who want a real agent kernel — not another chat wrapper.</sub>
</p>
