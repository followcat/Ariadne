# Web host: workspace, session, and account isolation

Status: **normative** (2026-07-23)

Aligned with **Codex / Grok personal-agent** mental models and Ariadne
`sandbox-v1` + CLI shell design. Complements [sandbox-v1.md](sandbox-v1.md),
[atelier.md](atelier.md), and [cli-shell-agent.md](cli-shell-agent.md).

## 1. Design thesis (Codex / Grok)

| Concept | Meaning |
| --- | --- |
| **Open project / 作坊树** | Durable files under `/workspace`. **Many chat threads share the same binding.** |
| **Conversation / session** | A thread of turns (transcript, title, memory scope). Switching chat does **not** clone the file tree. |
| **Atelier branch** | The **only** product surface that clones a full file tree for isolated hands-on work. |
| **Scratch** | Ephemeral files for one sandbox scope (`/session`). Not the project. |
| **Account (web)** | Identity for BYOK keys, plugins, memory, and **atelier roots** — **not** a multi-tenant SaaS tenancy. |

Codex and Grok coding UIs treat the **project as primary**, chat history as
secondary. Ariadne keeps that split. Multi-tenant control planes remain a
[non-goal](../NON_GOALS.md).

There is **no** serve-time `project | per_user` workspace-mode switch.
Binding is a single rule (§3).

```text
                    ┌─────────────────────────────────────┐
  durable files     │  /workspace  (open folder or 作坊)  │
                    └─────────────────────────────────────┘
                                      ▲
                                      │ shared by all chat sessions
                                      │ on that same binding
         ┌────────────────────────────┼────────────────────────────┐
         │ session A                  │ session B                  │
         │  transcript + title        │  transcript + title        │
         │  /session scratch (scope)  │  /session scratch (scope)  │
         └────────────────────────────┴────────────────────────────┘
```

## 2. Isolation matrix (web)

| Resource | Scope | Host location (typical) |
| --- | --- | --- |
| **`/workspace` files** | Open project **or** atelier main/branch (§3) | Serve cwd / `--workspace`, or atelier tree under user data |
| **`/session` scratch** | **user + sandbox scope** | `{user_data}/sandbox/<scope>/session` |
| Chat transcript / title | **user + session_id** | `{user_data}/sessions/` |
| Memory (state, summaries, semantic) | **user** (+ agent session id) | `{user_data}/memory/` (atelier may bind a scoped data_dir) |
| Provider (BYOK) | **user** | web users store |
| Official plugins config | **user** | `{user_data}/plugins.json` |
| Atelier projects | **user** | `{user_data}/ateliers/<slug>/` |

`user_data` = `{serve_data_dir}/web/users/{username}/` (web host).

Auth is required for workspace browse/file APIs. Authorization is **not**
“each file belongs to a SaaS tenant”; it is “logged-in personal host user.”

**Multiple local Web accounts on one `serve` share the open project tree** for
ordinary chats. Account isolation still covers `user_data` (memory, BYOK,
plugins, ateliers). That sharing is intentional for a personal single-machine
host — not a regression of multi-tenant productization (which is out of scope).

## 3. Single binding rule

`/workspace` is resolved once per request / turn:

| Context | `/workspace` root |
| --- | --- |
| Ordinary Web chat (no atelier selected) | `settings.workspace` — the folder opened at serve time (`cwd` / `--workspace`) |
| Atelier **main** | that atelier’s `workspace/` |
| Atelier **branch** | that branch’s `.ariadne/branch_workspaces/<slug>/` |

```bash
# Open this folder as the agent project (all ordinary chats share it)
cd ~/Projects/MyApp && ariadne serve
# or: ariadne serve --workspace ~/Projects/MyApp
```

Rules:

1. **One open folder** for ordinary Web. Matches CLI: cwd is the agent’s hands.
2. **Chat sessions never own `/workspace`.** `/new` / new chat keeps the same
   binding (CLI `/new` already: new session id, keep workspace).
3. **Atelier branch is the isolation unit for full tree copies** — not each chat.
4. **`/session` is never the web file browser root.** Browser shows durable
   `/workspace` only.
5. **Agent compose and HTTP browse use the same root** for a given turn
   (no silent split-brain).
6. **No dual mode flag.** Removed: `--workspace-mode`, `ARIADNE_WEB_WORKSPACE_MODE`,
   `Settings.web_workspace_mode`. Unknown remnants must not reintroduce a second path.

## 4. Docker / sandbox lifecycle

Default backend mounts the **active host root** at `/workspace` inside a
scope-bound container (see [sandbox-v1.md](sandbox-v1.md)):

- Model and tools see `/workspace` and ephemeral `/session`.
- Web file browser lists the **same** host tree (shows host path for orientation).
- Container lifetime is **sandbox scope** (start on use; stop/destroy on scope
  close or idle policy) — **not** “one full filesystem world per chat message.”
- Opening a new chat does **not** clone the project into a new Docker layer.

## 5. Path rewrite (chat → image / file)

Models often print **host absolute** paths (`/home/…/plot.png`) instead of
`/workspace/…`. The web host:

1. Accepts host paths **only if** they resolve under the active workspace root.
2. Exposes `workspace` (+ optional binding kind) on `GET /api/me` so the UI can
   map host paths to `/api/workspace/file?path=…`.
3. When browsing/serving with `atelier_id` / `atelier_session`, confining root
   is that atelier session tree.
4. Does not serve paths outside the active root (escape → 400).

## 6. API surface

| Endpoint | Binding |
| --- | --- |
| `GET /api/me` | `workspace` (active host root), `project_root` (serve open folder) |
| `GET /api/workspace/list\|read\|file` | Active root: open folder, or atelier main/branch when query params set |
| `POST /api/turns` (+ SSE) | `compose_agent` uses the **same** root for the turn |

Optional response field `workspace_binding` (or equivalent): `"project"` |
`"atelier"` — **derived** from whether an atelier is selected for that call,
not a user-configurable mode.

## 7. Non-goals (reaffirmed)

- Per-chat full workspace clones (would diverge from Codex project model).
- Serve-time `project | per_user` mode switch (removed).
- Cross-user ACLs, shared drives, org workspaces.
- Mandatory multi-tenant control plane.
- Treating web accounts as enterprise tenants.
- “Docker auto-destroy every chat open” as a full independent file world.

## 8. Implementation map

| Piece | Location |
| --- | --- |
| Open folder | `Settings.workspace`, serve cwd / `--workspace` |
| Resolve root | `ariadne.web.app` `_workspace_root_for(username, atelier_id=…, atelier_session=…)` |
| Agent binding | `_settings_for` / `_settings_for_atelier` set `workspace=` to that root |
| UI | left rail **工作区** tab; labels open-project vs 作坊/旁支 + host path |
| Sandbox contract | [sandbox-v1.md](sandbox-v1.md) `/workspace` + `/session` |
| Atelier trees | [atelier.md](atelier.md) |

## 9. Acceptance checks

1. Two chat sessions, same user, no atelier: write `/workspace/a.txt` in A →
   visible in B and in the workspace browser.
2. Two users on the same `serve`, ordinary chat: same open-folder path → same
   host file (documented sharing of the open project).
3. With `atelier_id` + branch session: list/file root is the branch tree, not
   main `workspace/` and not the serve open folder.
4. `/session` files do not appear in the workspace browser.
5. Host absolute path under active root loads as chat image; path outside → 400.
6. No `--workspace-mode` / `ARIADNE_WEB_WORKSPACE_MODE` product path.
