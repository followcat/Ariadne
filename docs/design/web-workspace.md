# Hosts: workspace, session, and identity (CLI vs Web)

Status: **normative** (2026-07-23)

Complements [sandbox-v1.md](sandbox-v1.md), [atelier.md](atelier.md), and
[cli-shell-agent.md](cli-shell-agent.md). Multi-tenant SaaS control planes remain a
[non-goal](../NON_GOALS.md).

## 1. Dual-host identity

Ariadne is one **kernel**. **CLI** and **Web** are separate **hosts** with different
identity and workspace stories. They do **not** share “who is the user” or a single
product “项目” object.

| | **CLI host** | **Web host** |
| --- | --- | --- |
| **Identity** | **Linux user** (`uid` / `$HOME` / file permissions) | **Registered account** (token under `web/users/<name>/`) |
| **Multi-account** | Switch OS user → different home & paths | Switch login → different `user_data` |
| **Workspace** | Folder you open (`cwd` / `--workspace`) — that user’s hands | **作坊 main/branch** for durable per-account files; ordinary file tab = serve host directory (see §3) |
| **Sessions** | Chat threads under CLI data dir | Chat threads under that Web account |
| **Cross-host** | Does not use Web registered accounts | Does not treat CLI cwd as each account’s private product “project” |

```text
                 Ariadne kernel
        (compose / turn / tools / memory)
                      │
         ┌────────────┴────────────┐
         │                         │
    CLI host                    Web host
  identity = Linux            identity = register
  workspace = open folder     作坊 = per-account files
  data ≈ ~/.ariadne or        data = {serve}/web/users/<name>/
           project .ariadne
```

**Import / embed:** `compose_agent(settings)` stays single-process, single
`Settings.workspace` — the “one person” path. No Web account required.

## 2. Concepts (not “Web 项目 = session”)

| Concept | Meaning |
| --- | --- |
| **Chat session** | One conversation thread (transcript, title). **Not** a filesystem. |
| **`/workspace` binding** | Durable host directory mounted for tools + file browser. Many sessions may share it. |
| **作坊 (atelier)** | Web product place with a name: main tree + optional **旁支** (full-tree isolate). Primary multi-account durable isolation on Web. |
| **旁支 (branch)** | Snapshot copy for hands-on work; not git; not each chat. |
| **Scratch `/session`** | Ephemeral sandbox scope files — never the browser root. |

**Web product surfaces (UI):** 历史 · 工作区 · 作坊.  
There is **no** first-class Web **「项目」** entity (no project picker, no project list).
API may still use internal keys such as `workspace_binding: "project"` meaning
**serve open-folder** — that is **not** a user-facing product name and is **not** a session.

## 3. Web `/workspace` binding (single rule)

No serve-time `project | per_user` mode switch (`--workspace-mode` /
`ARIADNE_WEB_WORKSPACE_MODE` removed).

| Context | `/workspace` root |
| --- | --- |
| Ordinary Web chat (no atelier selected) | Serve open folder (`settings.workspace` = cwd / `--workspace`) |
| Atelier **main** | that atelier’s `workspace/` under the account |
| Atelier **branch** | `.ariadne/branch_workspaces/<slug>/` for that branch |

```bash
# Ordinary Web: host directory for non-atelier chats (shared by chats on this serve)
cd ~/Projects/MyApp && ariadne serve
# Per-account durable work: use 作坊 in the UI (or atelier APIs)
```

Rules:

1. **Chat sessions never own `/workspace`.** New chat keeps the same binding.
2. **作坊旁支** is the isolation unit for full tree copies — not each chat.
3. **Multiple Web accounts** share the **same serve open folder** for ordinary
   (non-atelier) chats. Per-account file isolation is **作坊-centric** (and
   `user_data` for memory / BYOK / plugins). Documented, not a multi-tenant bug.
4. Agent compose and HTTP browse use the **same** root for a given turn.
5. UI **工作区** tab shows the active tree + host path; labels say **当前目录** /
   **作坊主线/旁支**, not a product 「项目」.

## 4. Isolation matrix (Web account)

| Resource | Scope | Typical host location |
| --- | --- | --- |
| Ordinary `/workspace` | Serve open folder | Process cwd / `--workspace` |
| 作坊 `/workspace` | Account + atelier main/branch | `{user_data}/ateliers/<slug>/…` |
| `/session` scratch | User + sandbox scope | `{user_data}/sandbox/<scope>/session` |
| Chat transcript | User + session_id | `{user_data}/sessions/` |
| Memory | User (+ agent session id) | `{user_data}/memory/` (atelier may scope data_dir) |
| BYOK / plugins | User | web users store / `plugins.json` |

`user_data` = `{serve_data_dir}/web/users/{username}/`.

## 5. Docker / sandbox

Default backend mounts the **active host root** at `/workspace` (see
[sandbox-v1.md](sandbox-v1.md)):

- Model sees `/workspace` + ephemeral `/session`.
- Web file browser lists the same host tree (path for orientation).
- Container lifetime is **sandbox scope**, not one world per chat message.

## 6. Path rewrite & API

1. Host absolute paths accepted only under the active root.
2. `GET /api/me`: `workspace`, `project_root` (serve open folder), optional
   `workspace_binding` (`"project"` | `"atelier"`) — **derived**, not a mode switch.
3. `GET /api/workspace/*` + `atelier_id` / `atelier_session` → atelier tree.
4. Escape outside root → 400.

## 7. Non-goals

- Web product “项目” equal to session or a project manager UI.
- Per-chat full workspace clones.
- Serve-time dual workspace modes.
- CLI using Web registered accounts (or vice versa as the same identity).
- Enterprise multi-tenant ACLs / org drives.

## 8. Implementation map

| Piece | Location |
| --- | --- |
| CLI open folder | `Settings.workspace`, cwd / `--workspace` |
| Web resolve root | `ariadne.web.app` `_workspace_root_for(…)` |
| UI | 历史 · **工作区** · **作坊**; labels 当前目录 vs 作坊 |
| Atelier | [atelier.md](atelier.md) |

## 9. Acceptance checks

1. Two chat sessions, same Web user, no atelier: same `/workspace` files.
2. Two Web users, ordinary chat: same serve open-folder host path (documented share).
3. `atelier_id` + branch: root ≠ main tree ≠ serve open folder.
4. `/session` not in workspace browser.
5. No live `--workspace-mode` / `ARIADNE_WEB_WORKSPACE_MODE` product path.
6. UI ordinary binding copy does not brand 「项目」 as a product entity.
