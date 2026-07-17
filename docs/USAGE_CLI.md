# CLI Usage (Shell Agent)

Ariadne's basic usage is a **CLI shell agent** over the project directory.

## Setup

```bash
cd /path/to/your/project
cp /path/to/Ariadne/.env.example .env   # or use Ariadne repo .env
# BASE_URL=...
# API_KEY=...
# MODEL=grok-4.5

cd /path/to/Ariadne
python3 -m pip install -e .
# or: PYTHONPATH=src python3 -m ariadne.cli.main ...
```

## Commands

```bash
# one-shot
ariadne run "create NOTES.md with a one-line outline of this project"

# interactive
ariadne chat

# inspect
ariadne doctor
ariadne tools
```

## Flags

```text
--workspace PATH   default cwd (mapped to /workspace)
--session ID       conversation/transcript key
--sandbox local|null
--model NAME
--verbose
--json
```

## How it works

```text
your prompt
  -> Agent/TurnApplication
  -> model (OpenAI-compatible tools)
  -> sandbox_exec on LocalWorkdir (/workspace = project)
  -> final answer printed in terminal
```

See [design/cli-shell-agent.md](design/cli-shell-agent.md) for the full design.
