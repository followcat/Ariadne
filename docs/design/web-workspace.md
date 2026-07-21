# Web host: workspace, session, and account isolation

Status: **normative** (2026-07-21)

Aligned with **Codex / Grok personal-agent** mental models and Ariadne
`sandbox-v1` + CLI shell design. Complements [sandbox-v1.md](sandbox-v1.md)
and [cli-shell-agent.md](cli-shell-agent.md).

## 1. Design thesis (Codex / Grok)

| Concept | Meaning |
| --- | --- |
| **Project workspace** | The folder you “opened” (CLI: cwd / `--workspace`). Durable files. **Many chat threads share it.** |
| **Conversation / session** | A thread of turns (transcript, title). Switching chat does **not** clone the project tree. |
| **Scratch** | Ephemeral files for one sandbox scope (`/session`). Not the project. |
| **Account (web)** | Identity for BYOK keys, plugins, and memory — **not** a multi-tenant SaaS tenancy. |

Codex and Grok coding UIs treat the **project as primary**, chat history as
secondary. Ariadne keeps that split. Multi-tenant control planes remain a
[non-goal](../NON_GOALS.md).

```text
                    ┌─────────────────────────────────────┐
  durable project   │  /workspace  (project or per_user)  │
                    └─────────────────────────────────────┘
                                      ▲
                                      │ shared by all sessions
                                      │ of that workspace binding
         ┌────────────────────────────┼────────────────────────────┐
         │ session A                  │ session B                  │
         │  transcript + title        │  transcript + title        │
         │  /session scratch (scope)  │  /session scratch (scope)  │
         └────────────────────────────┴────────────────────────────┘
```

## 2. Isolation matrix (web v1)

| Resource | Scope | Host location (typical) |
| --- | --- | --- |
| **`/workspace` files** | See §3 mode | Project root **or** per-account tree |
| **`/session` scratch** | **user + sandbox scope** | `{user_data}/sandbox/<scope>/session` |
| Chat transcript / title | **user + session_id** | `{user_data}/sessions/` |
| Memory (state, summaries, semantic) | **user** | `{user_data}/memory/` |
| Provider (BYOK) | **user** | web users store |
| Official plugins config | **user** | `{user_data}/plugins.json` |

`user_data` = `{serve_data_dir}/web/users/{username}/` (web host).

Auth is required for workspace browse/file APIs. Authorization is **not**
“each file belongs to a SaaS tenant”; it is “logged-in personal host user.”

## 3. Workspace modes

Configured at **serve** time (not per chat):

| Mode | `/workspace` root | When to use |
| --- | --- | --- |
| **`project`** (default) | `settings.workspace` (serve cwd / `--workspace`) | Codex-like: one project folder, all sessions (and accounts on this process) share durable files. Personal single-machine default. |
| **`per_user`** | `{user_data}/workspace/` | Multiple local accounts on one `serve` should not overwrite each other’s plots/scripts. Still personal; not multi-tenant productization. |

```bash
# project mode (default) — open this folder as the agent project
cd ~/Projects/MyApp && ariadne serve

# per-user durable trees under the host data dir
ariadne serve --workspace-mode per_user
# or: ARIADNE_WEB_WORKSPACE_MODE=per_user
```

Rules:

1. **Default is `project`.** Matches CLI: cwd is the agent’s hands.
2. **Sessions never own `/workspace`.** `/new` / new chat keeps the same
   workspace binding (CLI `/new` already: new session id, keep workspace).
3. **`/session` is never the web file browser root.** Browser shows durable
   `/workspace` only (Codex file tree = project).
4. **Agent compose and HTTP browse use the same root** for a given account
   and mode (no silent split-brain).
5. **Fastfail** on unknown mode. No silent fallback to another tree.

## 4. Path rewrite (chat → image / file)

Models often print **host absolute** paths (`/home/…/plot.png`) instead of
`/workspace/…`. The web host:

1. Accepts host paths **only if** they resolve under the active workspace root.
2. Exposes `workspace` + `workspace_mode` on `GET /api/me` so the UI can map
   host paths to `/api/workspace/file?path=…`.
3. Does not serve paths outside that root (escape → 400).

## 5. API surface

| Endpoint | Binding |
| --- | --- |
| `GET /api/me` | `workspace`, `workspace_mode`, `project_root` |
| `GET /api/workspace/list\|read\|file` | Active root for **current user** + mode |
| `POST /api/turns` (+ SSE) | `compose_agent` uses the **same** root |

`project_root` is always the serve process workspace (useful in `per_user`
mode to show “this account’s files live under data_dir, project root is …”
in the UI).

## 6. Non-goals (reaffirmed)

- Per-session full workspace clones (would diverge from Codex project model).
- Cross-user ACLs, shared drives, org workspaces.
- Mandatory multi-tenant control plane.
- Treating web accounts as enterprise tenants.

## 7. Implementation map

| Piece | Location |
| --- | --- |
| Mode setting | `Settings.web_workspace_mode`, env `ARIADNE_WEB_WORKSPACE_MODE`, `serve --workspace-mode` |
| Resolve root | `ariadne.web.app` `_workspace_root_for(username)` |
| Agent binding | `_settings_for` sets `workspace=` to that root |
| UI | left rail **工作区** tab; shows mode + host path |
| Sandbox contract | [sandbox-v1.md](sandbox-v1.md) `/workspace` + `/session` |

## 8. Acceptance checks

1. `project` mode: two sessions, same user, write `/workspace/a.txt` in A → visible in B and in browser.
2. `project` mode: two users, same file path → same host file (documented sharing).
3. `per_user` mode: user A write does not appear in user B’s list/root.
4. `/session` files do not appear in workspace browser.
5. Host absolute path under root loads as chat 走势图; path outside root → 400.
6. Unknown `web_workspace_mode` → config error at load/serve.
