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

- Web 完整 Atelier UI（可后续挂 project_id）  
- Git 自动 merge / PR  
- 替换 Memory L0–L4  
- 多租户 / 团队工坊  
- 强制 LLM 才能用（离线时知识库为手写文件）

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

## 7. Knowledge

Sections (template): 技术栈 / 关键决策 / 约定 / 经验教训 / 进行中的工作.

- **P0:** template + heuristic tree/README fill.  
- **P1:** optional LLM extract with evidence quotes.  
- Updates: snapshot to `knowledge_history/` then rewrite.

## 8. Related

- [web-workspace.md](web-workspace.md) — project files vs chat threads  
- [sandbox-v1.md](sandbox-v1.md) — /workspace vs /session  
- [cli-shell-agent.md](cli-shell-agent.md) — REPL host  
