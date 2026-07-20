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

Codex-style entry: **no subcommand → interactive CLI**. Subcommands are for
one-shot / admin. (Aligns with `codex [OPTIONS] [PROMPT]` vs `codex <COMMAND>`.)

### 2.1 Interactive REPL (default)

```bash
ariadne
ariadne "create notes.md with a short outline"   # REPL + first user turn
ariadne chat                                       # explicit alias of default
ariadne> /session
ariadne> /tools
ariadne> /exit
```

Defaults for interactive: `sandbox-lifecycle=active_session`, streaming on.
Non-TTY bare `ariadne` must not hang (print help / exit 2, or one-shot if a
prompt was provided on argv).

### 2.2 One-shot (non-interactive)

```bash
ariadne run "list python files and summarize what this repo does"
ariadne exec "…"    # alias of run
```

### 2.3 Session-bound workspace

```bash
ariadne --session demo --workspace . run "..."
# or
cd my-project && ariadne
ariadne -c                 # continue most recent session in REPL
ariadne resume --last      # same idea via subcommand
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

Anti-goal: a TUI product platform. **Rich host, thin kernel** — the CLI may be
a full-featured terminal agent (streaming, history, approvals, session
management); the kernel stays free of terminal concerns.

## 4. Command surface (v1)

```text
ariadne [PROMPT...]         # interactive REPL (default); optional first turn
ariadne chat                # alias of default interactive
ariadne run|exec PROMPT...  # one non-interactive turn
ariadne resume [id]         # list sessions, or enter REPL on id / --last
ariadne tools               # list exposed tools
ariadne sessions            # list recorded sessions
ariadne doctor              # check .env model + sandbox backend
ariadne plugins / plugin …  # official plugins (user attributes)
ariadne serve               # web UI
ariadne version
```

Global flags:

```text
--session ID          default: "local-" + hash(workspace)
--workspace PATH      default: cwd
--sandbox local|null|docker   default: local
--no-sandbox          force NullSandbox
--model NAME          override .env MODEL
--json                machine-readable TurnResult
--verbose / -v        show tool traces and layer reports
--tool-loop-limit N
--approval-mode auto|on-request|readonly   tool approval policy
-c / --continue       resume most recent session
--stream / --no-stream (interactive streams by default)
--no-welcome          suppress REPL banner
```

REPL meta-commands:

```text
/help
/exit | /quit
/status               # compact host status
/mode [auto|on-request|readonly]
/session
/workspace
/tools
/skills
/model [name]         # show or hot-swap model
/memory read
/usage                # cumulative tokens this REPL
/compact              # archive transcript, summaries keep history
/resume [id]          # list or switch sessions
/new | /reset-session # new session id, keep workspace
/sandbox-status
/clear-session-files  # wipe /session scratch (not /workspace)
/clear                # clear screen
```

REPL behavior: readline history persisted under the data dir; `\`
continuation and ``` fences for multiline input; Ctrl+C cancels the
current turn (sandbox cleanup guaranteed), only Ctrl+C at an empty
prompt exits.

## 4.1 Approval modes (host concern)

The kernel exposes `ToolContext.approval_hook`; the CLI hosts the policy:

| Mode | Behavior |
| --- | --- |
| `auto` (default) | all configured tools allowed |
| `on-request` | write-class tools (sandbox_exec, sandbox_write_file, sandbox_edit_file, skill_manage) prompt the user; reads pass |
| `readonly` | write-class tools always denied |

Denial maps to `ARIADNE_TOOL_DENIED` as an **error tool result** so the
model can explain or recover; the turn does not fail.

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
| REPL | bare `ariadne` (chat alias) | multi-turn coding; codex-style default entry |
