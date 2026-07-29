# Design: Atelier（工坊 / 小作坊）

Status: **normative for implementation** (2026-07-24)  
CLI: `ariadne atelier …`  
Module: `src/ariadne/atelier/`  
Web: `ariadne serve` → left tab **作坊**

Product kernel name: **Ariadne** / **筑梦师** (mythic thread + craft of dreams).  
Atelier is the workshop surface where that thread is practiced.

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
| Files (rw) | `workspace/` → sandbox `/workspace` | `.ariadne/branch_workspaces/<slug>/` → `/workspace`（创建时快照） |
| Main files (ro) | — | 主线 `workspace/` → sandbox **`/main-readonly`**（实时只读） |
| Memory / sandbox data | `.ariadne/` | `.ariadne/scopes/branch-<slug>/` |
| Transcript | own jsonl | own jsonl |
| KNOWLEDGE.md | 主线用户维护 | 只读参考 + `/workspace/KNOWLEDGE.md` 副本；merge **不写** 主线 |
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

## 7. Knowledge（小本本 / 本坊便签）

**Value:** 记**这间作坊怎么运作**——关键路径、运行方式、注意点。  
跨主线/旁支注入的短说明书；旁支只读。

| 写什么 | 不写什么 |
| --- | --- |
| 本坊目标与流程 | 整段聊天 transcript |
| `/workspace`、入口文件、输出位置等**路径** | 一次性临时命令输出 |
| 约束、坑、**注意** | Memory 级琐碎回忆 |
| 主线定下的运作约定 | 旁支私货当权威 |

| Do | Don't |
| --- | --- |
| 用户可随时手写/改 | 当第二套 Memory（细节 → L0–L4） |
| 始终注入（约 4k 字符上限） | 旁支或 merge 写主线便签 |
| 权威路径 = 作坊根 `KNOWLEDGE.md`（**不是** `/workspace/KNOWLEDGE.md`） | 假定沙箱能改根目录便签 |
| **主线 turn 后：运作/路径/注意类约定小步写入** | 每轮无脑重写全文 |

### 7.0 Main post-turn 更新（默认 on，保守）

```text
主线 turn 完成
  → 提取「运作 / 路径 / 注意 / 决策」类要点（启发式；可选 LLM）
  → 闲聊 → noop
  → 有 → 写入对应小节（本坊怎么运作 | 关键路径 | 注意）；history 快照
旁支 turn → 永不写便签
```

| 规则 | 要求 |
| --- | --- |
| Session | **仅 `main`** |
| 质量门 | 去重；拒过短/过长/纯问句；拒污染 JSON |
| 步幅 | 每轮少 ops；禁止整文件 free rewrite |
| 与 Memory | Memory 管细节；便签管**本坊运作说明书** |

模板小节：`本坊怎么运作` · `关键路径` · `注意` · `随手记`。  
根 `KNOWLEDGE.md` 权威；`workspace/KNOWLEDGE.md` 仅作空壳时的备选注入源。

## 7.1 Delivery + empty-reply

| Rule | Detail |
| --- | --- |
| System inject | 交付策略 + **当前会话** 文件树 +（旁支）主线 L1 摘要只读 |
| Empty content | Kernel 从 reasoning / 工具提示恢复，禁止静默空回复 |
| Thrash | 只读空转提醒；近上限强制收尾；循环耗尽中文说明 |
| 大写入 | 禁止超大单次 `sandbox_write_file`；小步改 |
| 出图 / 文档 | **以本坊便签「输出规范」为准**（画画→PNG、架构坊→md+svg 等）；旁支只读本坊便签，不作坊互通默认架构交付 |

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

- [web-workspace.md](web-workspace.md) — dual-host identity; open folder vs 作坊/旁支 (session ≠ workspace)
- [MEMORY.md](../MEMORY.md) / [memory-v1.md](memory-v1.md) — 分层记忆  
- [sandbox-v1.md](sandbox-v1.md) — /workspace vs /session  
- [cli-shell-agent.md](cli-shell-agent.md) — REPL host  
