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
ariadne toolbox
ariadne version
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
--sandbox-prestart            start sandbox in parallel with memory build
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
/memory read
/reset-session              # new session id, keep workspace
/sandbox-status
/clear-session-files
```

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
