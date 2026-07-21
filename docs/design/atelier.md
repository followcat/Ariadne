# Design: Atelier（工坊）

Status: **normative for implementation** (2026-07-21)  
CLI: `ariadne atelier …`  
Module: `src/ariadne/atelier/`

## 1. Naming

**Atelier** (法语：工作室 / 画室) — 一个项目工坊：

```text
共享工作台 (workspace)
知识墙 (KNOWLEDGE.md)
主工作台 (Main Session)
实验台 (Branch Session, optional)
```

Branch 是 **对话上下文隔离**，**不是** git branch。代码始终共享同一 workspace。

## 2. Goals

1. Codex-like：打开项目 → 连续对话 → 零 session 管理。  
2. 20% 场景：branch session 并行实验 → merge（摘要+知识）或 discard。  
3. 知识沉淀：`KNOWLEDGE.md` 结构化、可编辑、有 history。  
4. Host 产品层：复用 `compose_agent` / turn / sandbox；不平行 monogod。

## 3. Non-goals

- Git 自动 merge / PR  
- 替换 Memory L0–L4  
- 多租户 / 团队工坊  
- 强制 LLM 才能用（离线时知识库为手写文件 / 启发式）

## 4. Layout

Default root: `~/.ariadne/ateliers/<slug>/`  
Override: env `ARIADNE_ATELIER_ROOT`

```text
my-app/
├── project.yaml
├── KNOWLEDGE.md
├── workspace/              # or external path via config
├── skills/                 # optional
└── .ariadne/
    ├── sessions/
    │   ├── main.jsonl
    │   ├── main.meta.json
    │   └── branch-<name>.jsonl / .meta.json
    └── knowledge_history/
```

`--from PATH`: set `workspace_path` to absolute external directory (no full copy).

## 5. Isolation matrix

| Resource | Main | Branch |
| --- | --- | --- |
| Code files | shared workspace | **same** workspace |
| Transcript | own jsonl | own jsonl |
| Sandbox container | own scope | own scope |
| KNOWLEDGE.md | read + auto-update after turns | read; write on **merge** only |

## 6. CLI

```text
ariadne atelier create NAME [--from PATH] [--no-scan]
ariadne atelier list
ariadne atelier open NAME [--session ID]
ariadne atelier delete NAME [-y]
ariadne atelier branch create|list|merge|discard PROJECT NAME
ariadne atelier knowledge show|edit|refresh|history PROJECT
```

`open` → get_or_create main → existing REPL (`run_repl`) with workspace + session bound.

## 7. Knowledge (Codex AGENTS.md model)

**Value:** cross-session project continuity — a short user-owned brief, always injected.

| Do | Don't |
| --- | --- |
| User writes stable decisions / conventions | Auto-extract every turn (default **off**) |
| Always inject (capped, ~4k chars) | Treat as second Memory / full archive |
| Optional create-time tree scaffold | Rely on heuristic/LLM as source of truth |
| Merge appends a short note for humans to edit | Auto-merge “decisions” from branch dialogue |

Template is minimal (`决策与约定` / `备注`). Programmatic `apply_updates` / `extract_*` remain as **opt-in libraries** (tests / power tools), not the host default path.

**Root vs workspace:** canonical brief is `{atelier}/KNOWLEDGE.md`. If the root file is thin/polluted and `workspace/KNOWLEDGE.md` is richer, inject (and optional GET sync) prefers the workspace copy so agent-written notes are not lost.

Automatic recall / sedimentation → **Memory L0–L4** (CuratedStore, summaries, etc.).

## 7.1 Delivery + empty-reply (capability)

| Rule | Detail |
| --- | --- |
| System inject | Delivery policy + **workspace file tree** + (branch) optional main L1 summary |
| Agent session id | `aw-{project.id}-{session.id}` (no double `atelier-` prefix) |
| Empty model content | Kernel recovers from `reasoning_content` (capped) and/or tool-nudge text — never silent empty turn |
| Implement tasks | Prefer `sandbox_write_file` / edit; final reply must describe paths + how to verify |

## 8. Web UI

Per-account ateliers live under `{user_data}/ateliers/` (not multi-tenant SaaS).

| API | Role |
| --- | --- |
| `GET/POST /api/ateliers` | list / create |
| `GET/DELETE /api/ateliers/{id}` | detail / delete (`?yes=true`) |
| `GET …/sessions`, `…/sessions/{sid}/messages` | main + branch transcripts |
| `POST …/branches`, `…/branches/{name}/merge\|discard` | branch lifecycle |
| `GET/PUT …/knowledge` | project brief view/edit |
| `POST …/knowledge/apply\|refresh` | optional power API (not primary UX) |
| `POST /api/turns/stream` + `atelier_id` / `atelier_session` | turns + KNOWLEDGE inject (no auto-write) |

Vue: left tab **工坊** · **项目说明** panel (view / full markdown edit) · workspace browser with `atelier_id`.

## 9. Related

- [web-workspace.md](web-workspace.md) — project files vs chat threads  
- [sandbox-v1.md](sandbox-v1.md) — /workspace vs /session  
- [cli-shell-agent.md](cli-shell-agent.md) — REPL host  
- [web-vue-frontend.md](web-vue-frontend.md) — SPA shell  
