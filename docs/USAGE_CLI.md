# CLI & host usage

> Language: **English** · [简体中文](zh/USAGE_CLI.md)

Ariadne’s primary host is a **CLI shell agent** over a project workspace.  
**Bare `ariadne` enters interactive mode** (codex-style). Optional hosts: **web UI**
(`ariadne serve`) and the **Python** `Agent` API.

## Requirements

- Python **3.13+**
- An **OpenAI-compatible** chat-completions endpoint that supports tools (optional for offline tests)
- Optional: Docker (for `--sandbox docker`), Playwright (`pip install -e ".[dev]"` for web e2e)

## Install

```bash
git clone <repo-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
ariadne version
```

Without install (from the repo root):

```bash
export PYTHONPATH=$PWD/src
python3 -m ariadne doctor
```

## Configure the model

Ariadne loads credentials from, in order of merge:

1. Process environment  
2. Workspace `.env`  
3. Current working directory `.env`  
4. Ariadne package root `.env` (when developing the repo)

Never commit real keys. Start from the example:

```bash
cp .env.example .env
```

```bash
# OpenAI-compatible host (path should include /v1 if the server uses it)
BASE_URL=https://api.longcat.chat/openai/v1
API_KEY=your-key-here
MODEL=LongCat-2.0
```

Notes:

- Any compatible gateway works (LongCat, OpenAI, Azure-compatible proxies, local gateways, …).
- Aliases: `OPENAI_BASE_URL`, `OPENAI_API_KEY` are also accepted.
- For providers with “thinking” modes, prefer a normal `max_tokens` budget (CLI default is large enough). Tiny smoke tests with `max_tokens=16` may return empty `content` while reasoning tokens are spent.

Check configuration:

```bash
ariadne doctor
```

## Commands

```bash
# interactive REPL (default entry — codex-style)
ariadne
ariadne "create NOTES.md with a one-line outline of this project"  # REPL + first turn
ariadne chat                    # explicit alias of interactive
ariadne -c                      # continue most recent session in REPL
ariadne resume --last
ariadne resume                  # list sessions

# one-shot turn (cwd → /workspace), non-interactive
ariadne run "create NOTES.md with a one-line outline of this project"
ariadne exec "…"                # alias of run

# streaming tokens / turn events + verbose tool traces
ariadne --stream -v run "summarize README.md"

# inspect
ariadne doctor
ariadne tools
ariadne skills
ariadne skills validate     # strict pack validation
ariadne sessions
ariadne toolbox
ariadne version

# web UI — register users, bind each user’s own provider (BYOK)
ariadne serve --host 127.0.0.1 --port 8420

# official plugins (user attributes by default)
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token ...
ariadne plugin enable redmine --url https://redmine.example.com --api-key ...
ariadne plugin enable odoo --url https://odoo.example.com \
    --database db --login user --password ...
ariadne plugin disable gitlab
# optional project override (workspace data_dir/plugins.json wins on name clash)
ariadne plugin enable gitlab --workspace-scope --url ... --token ...
```

Global flags work both before and after the subcommand  
(`ariadne --session demo run "..."` and `ariadne run --session demo "..."`).

## Flags

```text
--workspace PATH              default: cwd (mapped to /workspace)
--session ID                  conversation / transcript key
                              (default: local-<hash(workspace)>)
--sandbox local|null|docker   execution backend
--no-sandbox                  alias for --sandbox null
--force-workspace             allow / or $HOME as workspace (refused by default)
--sandbox-lifecycle           per_turn | active_session
--toolbox PROFILE             minimal | docs | data
--docker-image IMAGE          override docker image
--model NAME                  override MODEL from env
--tool-loop-limit N           default: 32
--skills-dir PATH             extra skill packs
--eager-tools                 send all schemas (disable deferred)
--stream                      stream model deltas + turn events
--no-stream                   disable streaming in chat (chat streams by default)
--sandbox-prestart            start sandbox in parallel with memory build
--approval-mode MODE          auto | on-request | readonly tool approval
-c / --continue               resume the most recent session (interactive or run)
--no-welcome                  suppress interactive banner
--no-stream                   disable streaming in interactive mode
-v / --verbose                tool traces, usage, schema metrics
--json                        print TurnResult JSON (includes schema_metrics);
                              with --stream, NDJSON events then final result
```

## Interactive meta-commands

```text
/help
/exit /quit
/status                     compact host status
/mode [auto|on-request|readonly]
/session
/workspace
/tools
/skills
/model [name]               show or hot-swap model
/memory read
/usage                      cumulative tokens this REPL
/compact                    archive transcript (summaries keep history)
/resume [id]                list or switch sessions
/new | /reset-session       new session id, keep workspace
/sandbox-status
/clear-session-files
/clear
```

REPL notes: history persists under the data dir; `\` continuation and  
\`\`\` fences give multiline input; Ctrl+C cancels the running turn (the  
sandbox is still cleaned up); Ctrl+C at an empty prompt exits.  
Non-TTY bare `ariadne` does not hang (prints help, or runs one-shot if a prompt was given).

## File tools

Besides `sandbox_exec`, the model can use structured file tools:

```text
sandbox_read_file   {path}
sandbox_write_file  {path, content}                 → unified diff
sandbox_edit_file   {path, old_string, new_string}  → exact once, unified diff
```

Edits fastfail when `old_string` matches zero or multiple times; the CLI  
renders returned diffs with syntax highlighting.

## Plugins (user attributes)

Plugin credentials are **owned by the user**, not by a multi-company pack system.

| Host | Default store | Notes |
| --- | --- | --- |
| CLI | `~/.ariadne/plugins.json` (mode `0600`) | Cross-workspace. Use `--workspace-scope` for project `data_dir/plugins.json`. Compose merges **user → workspace** (workspace wins on name). |
| Web | `data_dir/web/users/<username>/plugins.json` | Per registered account. API: `GET/PUT/DELETE /api/me/plugins`. Home merge is **off** for web. |

```bash
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token glpat-…
ariadne plugin disable gitlab
```

## Web UI

```bash
ariadne serve --port 8420
# → http://127.0.0.1:8420
```

Typical flow:

1. Register a username / password  
2. Bind provider (`BASE_URL`, `API_KEY`, `MODEL`) under **Provider**  
3. Optionally enable official plugins under **插件**  
4. Chat with SSE streaming turns  

End-to-end browser check (Playwright):

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH=src python3 scripts/verify_web.py
```

## Guardrails & approval

- **In/out redaction** — secrets matching common patterns are redacted before model input / transcript and on assistant output; injection-like phrases emit `guard_finding` warnings (warn, do not hard-block).  
- **Approval modes** — `--approval-mode auto` (default), `on-request` (prompt before mutating tools), `readonly` (deny writes). Denied tools surface as `ARIADNE_TOOL_DENIED` so the model can recover.

## How it works

```text
your prompt
  → Agent / TurnApplication
  → memory layers + skill index
  → model (OpenAI-compatible tools; optional stream)
  → sandbox tools on /workspace and /session
  → projection job enqueue + semantic index
  → final answer in terminal or web SSE
```

See [design/cli-shell-agent.md](design/cli-shell-agent.md) for the full CLI design,  
the root [README.md](../README.md) (English) / [README.zh-CN.md](../README.zh-CN.md) (中文) for screenshots,  
and [I18N.md](I18N.md) for the bilingual docs policy.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `BASE_URL` / `API_KEY` missing | `ariadne doctor`; `.env` path and variable names |
| HTTP 401 from model | Key validity; `Authorization: Bearer …` expected by host |
| Empty assistant text on tiny smoke tests | Provider “thinking” may consume a small `max_tokens`; raise the limit |
| Proxy breaks localhost tests | Unset `HTTP_PROXY` / `HTTPS_PROXY` for local fake servers and Playwright |
| Workspace refused | Ariadne refuses `/` and `$HOME` unless `--force-workspace` |
