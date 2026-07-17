# Design: CLI as the Primary Shell-Agent Interface

Status: **active / normative for personal usage**  
Related: [../PUBLIC_API.md](../PUBLIC_API.md), [sandbox-v1.md](sandbox-v1.md), [../ARCHITECTURE.md](../ARCHITECTURE.md)

## 1. Intent

Ariadne's default human interface is a **CLI shell agent**:

```text
user types in terminal
  -> ariadne CLI host
  -> TurnApplication (kernel)
  -> model + tools (sandbox.exec, memory, skills...)
  -> results print back to terminal
```

Library `Agent.run(...)` remains the programmatic API.  
CLI is not a second agent implementation — it is a **host** over the same kernel.

## 2. Product shape

### 2.1 One-shot

```bash
ariadne run "list python files and summarize what this repo does"
```

### 2.2 Interactive REPL

```bash
ariadne chat
ariadne> create notes.md with a short outline
ariadne> /session
ariadne> /tools
ariadne> /exit
```

### 2.3 Session-bound workspace

```bash
ariadne --session demo --workspace . run "..."
# or
cd my-project && ariadne chat
```

Default mental model: **current project directory is the agent's hands**;  
the model works mainly through `sandbox.exec` in that workspace.

## 3. Why CLI-first for shell agents

| Goal | CLI behavior |
| --- | --- |
| Agent that uses a computer | map host CWD/workspace into sandbox `/workspace` |
| Low ceremony personal use | zero HTTP/connectors required |
| Debuggable tool loop | print tool calls, exit codes, truncated outputs |
| Same kernel as library | CLI only formats events / reads argv |

Anti-goal: a TUI product platform. Keep CLI thin.

## 4. Command surface (v1)

```text
ariadne run   PROMPT...     # one turn
ariadne chat                # multi-turn REPL
ariadne tools               # list exposed tools
ariadne doctor              # check .env model + sandbox backend
ariadne version
```

Global flags:

```text
--session ID          default: "default" or directory hash
--workspace PATH      default: cwd
--sandbox local|null|docker   default: local
--model NAME          override .env MODEL
--json                machine-readable TurnResult
--verbose / -v        show tool traces and layer reports
--tool-loop-limit N
--no-sandbox          force NullSandbox
```

REPL meta-commands:

```text
/help
/exit | /quit
/session
/workspace
/tools
/memory read
/reset-session          # new session id, keep workspace
/clear-session-files    # wipe /session scratch (not /workspace)
```

## 5. Host architecture

```text
ariadne.cli
  parse argv / REPL lines
  load .env (BASE_URL, API_KEY, MODEL)
  compose:
     ModelPort (OpenAI-compatible chat)
     LocalWorkdirSandbox(workspace=...)
     Memory (L0 transcript under .ariadne/)
     ToolRegistry (sandbox.exec + memory later + skills later)
     TurnApplication / Agent
  render TurnEvent stream to terminal
```

Composition root lives in CLI (or `ariadne.host.compose`).  
Kernel packages stay free of argparse/rich.

## 6. Terminal UX contract

### 6.1 Default human output

```text
$ ariadne run "create hello.txt with hi"
• tool sandbox.exec
  $ printf 'hi\n' > /workspace/hello.txt
  exit 0
• tool sandbox.exec
  $ cat /workspace/hello.txt
  hi
  exit 0

hi — wrote hello.txt in the workspace.
```

Rules:

1. Final assistant text is primary.
2. Tool calls are secondary, indented, and optional via quiet mode later.
3. Huge stdout shows compression/truncation markers from sandbox.
4. Errors use `ARIADNE_*` codes + short message; non-zero process exit on failure.

### 6.2 JSON mode

`--json` prints one `TurnResult` object (and no decorative bullets).  
For streaming, NDJSON events then a final result object (phase 2).

### 6.3 Streaming (phase 2)

Print model deltas live; tool events interrupt cleanly.  
v1 may buffer per model exchange if simpler.

## 7. Workspace binding (critical)

```text
host workspace PATH
  -> LocalWorkdirSandbox maps PATH to /workspace
  -> /session is ephemeral under .ariadne/sandbox/<scope>/session
```

Policy:

- Default workspace = process CWD when CLI starts
- Refuse to use `/` or obviously huge system roots without `--force-workspace`
- Prefer relative paths in model-facing hints (`/workspace/...`)
- Do not mount `$HOME` wholesale

This makes the CLI feel like: **an agent sitting inside your project shell**.

## 8. Session identity

```text
session_id:
  - flag --session
  - else env ARIADNE_SESSION
  - else "local-" + short hash(workspace)   # stable per project
```

Transcript/memory keys use `session_id`.  
Sandbox scope key uses `session_id` (+ turn id for per_turn).

## 9. Config loading

Order (low → high):

```text
built-in defaults
  -> .env in cwd or parents (BASE_URL, API_KEY, MODEL)
  -> env vars
  -> CLI flags
```

Missing API key → CLI exits with clear doctor-style message (fastfail).

## 10. Relation to sandbox design

CLI is the **primary consumer** of:

- `LocalWorkdirSandbox`
- `per_turn` default
- later `active_session` for `ariadne chat` long coding sessions

`ariadne chat` should eventually default toward `active_session` so multi-turn file work keeps `/session` warm; `ariadne run` stays `per_turn`.

## 11. Relation to public API

```python
# CLI essentially does:
agent = compose_from_env(workspace=..., sandbox=...)
result = await agent.run(prompt, session_id=...)
print_human(result)  # or json
```

No separate tool loop in CLI.

## 12. Implementation phases

### C0 — Docs + argv shape (this doc)
### C1 — `ariadne run` with model + sandbox.exec only
### C2 — `ariadne chat` REPL
### C3 — verbose traces, doctor, tools list
### C4 — active_session for chat, streaming
### C5 — skills/memory wired into same CLI

## 13. Acceptance

1. From a git repo: `ariadne run "create NOTES.md with one line"` creates `NOTES.md` on disk.
2. `ariadne chat` can do two follow-up turns that reference prior file.
3. `--json` emits parseable TurnResult with tool_calls.
4. Without `.env`, command fails with setup guidance (no hang).
5. Kernel unit tests do not depend on CLI; CLI tests use the same Agent façade.

## 14. Decision record

| Decision | Choice | Why |
| --- | --- | --- |
| Primary UX | CLI shell agent | personal open-source DX |
| Core entry | same Agent/TurnApplication | no dual implementation |
| Default hands | sandbox.exec on workspace | real computer use |
| Default workspace | cwd | feels like project shell |
| REPL | `ariadne chat` | multi-turn coding |
