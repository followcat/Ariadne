# CLI Usage (Shell Agent)

Ariadne's primary host is a **CLI shell agent** over a project workspace.

## Setup

```bash
cd /path/to/your/project
# Put LLM credentials where Ariadne can find them (workspace `.env` or Ariadne repo `.env`)
# BASE_URL=...
# API_KEY=...
# MODEL=grok-4.5

cd /path/to/Ariadne
python3 -m pip install -e ".[dev]"
# or without install:
PYTHONPATH=src python3 -m ariadne ...
```

## Commands

```bash
# one-shot turn
ariadne run "create NOTES.md with a one-line outline of this project"

# interactive multi-turn (defaults sandbox lifecycle to active_session)
ariadne chat

# streaming tokens / turn events
ariadne --stream -v run "summarize README.md"

# inspect
ariadne doctor
ariadne tools
ariadne skills
ariadne skills validate     # strict pack validation
ariadne sessions
ariadne toolbox
ariadne version

# web UI
ariadne serve --port 8420   # register users, bind their own provider (BYOK)

# official plugins (user attributes by default)
# enable writes ~/.ariadne/plugins.json (mode 0600), cross-workspace
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token ...
ariadne plugin enable redmine --url https://redmine.example.com --api-key ...
ariadne plugin enable odoo --url https://odoo.example.com \
    --database db --login user --password ...
ariadne plugin disable gitlab
# optional project override (workspace data_dir/plugins.json wins on name clash)
ariadne plugin enable gitlab --workspace-scope --url ... --token ...

# web UI: ariadne serve — each registered user has their own plugins under
# data_dir/web/users/<username>/plugins.json (API: /api/me/plugins)
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
-c / --continue               resume the most recent session
-v / --verbose                tool traces, usage, schema metrics
--json                        print TurnResult JSON (includes schema_metrics);
                              with --stream, NDJSON events then final result
```

## Chat meta-commands

```text
/help
/exit /quit
/session
/workspace
/tools
/skills
/model [name]               show or hot-swap model
/memory read
/usage                      cumulative tokens this REPL
/compact                    archive transcript (summaries keep history)
/resume [id]                list or switch sessions
/reset-session              new session id, keep workspace
/sandbox-status
/clear-session-files
/clear
```

REPL notes: history persists under the data dir; `\` continuation and
``` fences give multiline input; Ctrl+C cancels the running turn (the
sandbox is still cleaned up), Ctrl+C at an empty prompt exits.

## File tools

Besides `sandbox_exec`, the model can use structured file tools:

```text
sandbox_read_file   {path}
sandbox_write_file  {path, content}                 -> unified diff
sandbox_edit_file   {path, old_string, new_string}  -> exact once, unified diff
```

Edits fastfail when `old_string` matches zero or multiple times; the CLI
renders returned diffs with syntax highlighting.

## How it works

```text
your prompt
  -> Agent / TurnApplication
  -> memory layers + skill index
  -> model (OpenAI-compatible tools; optional stream)
  -> sandbox_exec on /workspace and /session
  -> projection job enqueue + semantic index
  -> final answer in terminal
```

See [design/cli-shell-agent.md](design/cli-shell-agent.md) for the full design.
