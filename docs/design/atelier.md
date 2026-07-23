# Design: Atelier（工坊 / 小作坊）

Status: **normative for implementation** (2026-07-23)  
CLI: `ariadne atelier …`  
Module: `src/ariadne/atelier/`  
Web: `ariadne serve` → left tab **作坊**

## 1. Naming

**Atelier** (法语：工作室 / 画室) — 产品层「小作坊」：

```text
小作坊 = 主线工作台 + 旁支实验台(可选) + 小本本
├── 主线 workspace/          # 主线独占文件
├── KNOWLEDGE.md             # 主线小本本（用户维护）
├── 旁支 branch_workspaces/  # 每旁支一份拷贝，互不影响
└── 主线 / 旁支 各自会话与记忆
```

Branch **不是** git branch。  
旁支 = **独立文件树 + 独立记忆 + 独立对话**（创建时从主线快照拷贝）。

## 2. Goals

1. Codex-like：打开项目 → 主线连续对话 → 零 session 管理。  
2. 旁支：动手实验不污染主线；「收」归档摘要、「丢」删旁支树。  
3. 小本本：`KNOWLEDGE.md` 用户主导、始终注入（类似 AGENTS.md）。  
4. Host 产品层：复用 `compose_agent` / turn / sandbox；不平行 monogod。

## 3. Non-goals

- Git 自动 merge / PR / 自动把旁支文件推广回主线  
- 替换 Memory L0–L4（自动记忆仍归 Memory）  
- 多租户 / 团队工坊  
- 强制 LLM 才能用（离线时小本本为手写文件）

## 4. Layout

Default root (CLI): `~/.ariadne/ateliers/<slug>/`  
Override: env `ARIADNE_ATELIER_ROOT`  
Web: `{data}/web/users/<user>/ateliers/<slug>/`

```text
my-app/
├── project.json / project.yaml
├── KNOWLEDGE.md                 # 主线小本本
├── workspace/                   # 主线文件（旁支改不到）
├── skills/                      # optional
└── .ariadne/
    ├── sessions/
    │   ├── main.jsonl + .meta.json
    │   └── branch-<name>.jsonl + .meta.json
    ├── knowledge_history/
    ├── branch_workspaces/
    │   └── <slug>/              # 旁支独占文件（创建时从 workspace 拷贝）
    └── scopes/
        └── branch-<slug>/       # 旁支 memory / sandbox data
```

`--from PATH`: main `workspace_path` 指向外部目录（不整仓复制）；旁支仍拷贝该树的一份快照。

## 5. Isolation matrix

| Resource | Main | Branch |
| --- | --- | --- |
| Files | `workspace/` | `.ariadne/branch_workspaces/<slug>/`（快照拷贝） |
| Memory / sandbox data | `.ariadne/` | `.ariadne/scopes/branch-<slug>/` |
| Transcript | own jsonl | own jsonl |
| KNOWLEDGE.md | 主线用户维护 | 只读参考；merge **不写** 主线 |
| Agent session id | `aw-{id}-main` | `aw-{id}-branch-<slug>` |
| max_tokens | ≥ global default | 至少 **16384**（实现向） |

**UX 角色：**  
- **主线** = 策略 / 工作定义 / 取舍（少写大段实现）。  
- **旁支** = 动手：改代码、出图。  
创建旁支 = 拷贝主线文件；丢 = 删旁支树 + scope；收 = 仅旁支侧摘要归档，**不自动推广文件**。

## 6. CLI

```text
ariadne atelier create NAME [--from PATH] [--no-scan]
ariadne atelier list
ariadne atelier open NAME [--session ID]
ariadne atelier delete NAME [-y]
ariadne atelier branch create|list|merge|discard PROJECT NAME
ariadne atelier knowledge show|edit|refresh|history PROJECT
```

`open` → bind **session workspace** + `extra_system_prompt` → `run_repl`。

## 7. Knowledge（小本本）

**Value:** 跨会话项目名片；用户手写为主。

| Do | Don't |
| --- | --- |
| 用户写稳定决策 / 约定 | 每轮自动提取（默认 **off**） |
| 始终注入（约 4k 字符上限） | 当第二套 Memory |
| 创建时可选文件树脚手架 | 把旁支 merge 自动灌进小本本 |

模板偏口语（「我想记住的 / 随手记」）。  
根 `KNOWLEDGE.md` 为主；若根文件空壳/污染而 `workspace/KNOWLEDGE.md` 更充实，注入可优先 workspace 副本（GET 时可 sync）。

自动沉淀 → **Memory L0–L4**。

## 7.1 Delivery + empty-reply

| Rule | Detail |
| --- | --- |
| System inject | 交付策略 + **当前会话** 文件树 +（旁支）主线 L1 摘要只读 |
| Empty content | Kernel 从 reasoning / 工具提示恢复，禁止静默空回复 |
| Thrash | 只读空转提醒；近上限强制收尾；循环耗尽中文说明 |
| 大写入 | 禁止超大单次 `sandbox_write_file`；小步改 |
| 出图 | 写到当前 `/workspace`，并用 `![…](/workspace/…)` 展示 |

## 7.2 Tokens

| 场景 | max_tokens |
| --- | --- |
| 全局默认 | **8192**（`ARIADNE_MAX_TOKENS`） |
| 作坊 turn | **至少 16384** |

这是单次补全输出上限，不是上下文窗口。

## 8. Web UI

| API | Role |
| --- | --- |
| `GET/POST /api/ateliers` | list / create |
| `GET/DELETE /api/ateliers/{id}` | detail / delete (`?yes=true`) |
| sessions / messages | 主线与旁支 transcript |
| branches create / merge / discard | 旁支生命周期 |
| knowledge GET/PUT | 小本本 |
| `POST /api/turns/stream` + `atelier_id` + `atelier_session` | 绑定会话 workspace |
| workspace list/read/file + `atelier_id` + `atelier_session` | 浏览/内联图片（旁支目录） |

Vue：**作坊** 页 · 小本本面板 · 工作区浏览器带 session 作用域。  
切换对话：历史默认最近 80 条、Abort 取消、图片 blob 缓存。

## 9. Related

- [web-workspace.md](web-workspace.md) — open folder vs 作坊/旁支 binding
- [MEMORY.md](../MEMORY.md) / [memory-v1.md](memory-v1.md) — 分层记忆  
- [sandbox-v1.md](sandbox-v1.md) — /workspace vs /session  
- [cli-shell-agent.md](cli-shell-agent.md) — REPL host  
