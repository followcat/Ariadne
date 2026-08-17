# CLI 与宿主用法

> 语言：[English](../USAGE_CLI.md) · **简体中文**

Ariadne 的主宿主是面向「你打开的文件夹」的 **CLI 终端 Agent**。  
**CLI 身份 = Linux 用户**（home、权限、cwd）。  
**直接运行 `ariadne` 即进入交互模式**（对齐 codex 入口）。可选宿主：**Web UI**
（`ariadne serve`，**注册账号**，作坊承载账号级文件）与 **Python** `Agent` API。  
CLI 与 Web **不共享**同一个产品层「项目」对象。

## 环境要求

- Python **3.13+**
- **OpenAI 兼容**的 chat-completions 端点，建议支持 tools（离线测试可不需要）
- 可选：Docker（`--sandbox docker`）、Playwright（`pip install -e ".[dev]"` 做 Web e2e）

## 安装

```bash
git clone <repo-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
ariadne version
```

不安装包时（在仓库根目录）：

```bash
export PYTHONPATH=$PWD/src
python3 -m ariadne doctor
```

## 配置模型

凭据按以下顺序合并加载：

1. 进程环境变量  
2. 工作区 `.env`  
3. 当前工作目录 `.env`  
4. 开发 Ariadne 时的包根目录 `.env`

切勿提交真实密钥。从示例开始：

```bash
cp .env.example .env
```

```bash
# OpenAI 兼容主机（若服务端带 /v1，请写在 BASE_URL 里）
BASE_URL=https://api.longcat.chat/openai/v1
API_KEY=your-key-here
MODEL=LongCat-2.0
```

说明：

- 任意兼容网关均可（LongCat、OpenAI、Azure 兼容代理、本地网关等）。
- 也接受别名：`OPENAI_BASE_URL`、`OPENAI_API_KEY`。
- 带「thinking / 思考」模式的 Provider，请给足 `max_tokens`（CLI 默认足够大）。`max_tokens=16` 一类极小烟测可能只剩 reasoning、正文 `content` 为空。

检查配置：

```bash
ariadne doctor
```

## 命令

```bash
# 交互 REPL（默认入口，对齐 codex）
ariadne
ariadne "create NOTES.md with a one-line outline of this project"  # REPL + 首轮
ariadne chat                    # 交互模式的显式别名
ariadne -c                      # 在 REPL 中续最近会话
ariadne resume --last
ariadne resume                  # 列出会话

# 非交互单轮（cwd → /workspace）
ariadne run "create NOTES.md with a one-line outline of this project"
ariadne exec "…"                # run 的别名

# 流式 token / turn 事件 + 详细工具轨迹
ariadne --stream -v run "summarize README.md"

# 查看与自检
ariadne doctor
ariadne tools
ariadne skills
ariadne skills validate     # 严格校验 skill pack
ariadne sessions
ariadne toolbox
ariadne version

# Web UI — 注册账号 + BYOK；作坊 = 账号级 durable 文件；普通工作区 = serve 主机目录
ariadne serve --host 127.0.0.1 --port 8420
# 双宿主身份：docs/design/web-workspace.md

# 官方插件（默认写入用户属性）
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token ...
ariadne plugin enable redmine --url https://redmine.example.com --api-key ...
ariadne plugin enable odoo --url https://odoo.example.com \
    --database db --login user --password ...
ariadne plugin disable gitlab
# 可选：项目级覆盖（工作区 data_dir/plugins.json 同名优先）
ariadne plugin enable gitlab --workspace-scope --url ... --token ...
```

全局 flag 可写在子命令前或后  
（`ariadne --session demo run "..."` 与 `ariadne run --session demo "..."` 等价）。

## Flags

```text
--workspace PATH              默认：cwd（映射到 /workspace）
--session ID                  会话 / transcript 键
                              （默认：local-<hash(workspace)>）
--sandbox local|null|docker   执行后端
--no-sandbox                  等同 --sandbox null
--force-workspace             允许把 / 或 $HOME 当工作区（默认拒绝）
--sandbox-lifecycle           per_turn | active_session
--toolbox PROFILE             minimal | docs | data
--docker-image IMAGE          覆盖 docker 镜像
--model NAME                  覆盖环境里的 MODEL
--tool-loop-limit N           默认：32
--skills-dir PATH             额外 skill packs
--eager-tools                 发送全部 schema（关闭 deferred）
--stream                      流式模型 delta + turn 事件
--no-stream                   chat 中关闭流式（chat 默认开流式）
--task                        本轮强制闭环 task 模式（plan/verify）
--task-mode-policy MODE       off | on | auto（默认 auto）
                              环境变量：ARIADNE_TASK_MODE_POLICY
--sandbox-prestart            与 memory build 并行预热沙箱
--approval-mode MODE          auto | on-request | readonly 工具审批
-c / --continue               续最近一次会话（交互或 run）
--no-welcome                  隐藏交互欢迎横幅
--no-stream                   交互模式关闭流式
-v / --verbose                工具轨迹、用量、schema 指标
--json                        输出 TurnResult JSON（含 schema_metrics）；
                              配合 --stream 时先 NDJSON 事件再最终结果
```

### 闭环 task 模式（Phase 14）

计划 → 执行 → 验证 → 重规划。设计见 [design/agent-closed-loop.md](../design/agent-closed-loop.md)。

```bash
# 本轮强制 task 模式（模型需 submit_task_plan，且步骤带 done_when）
ariadne --task run "补一个冒烟测试并跑通"

# 策略（默认 auto）：有进行中的 task 时自动恢复，不必每轮再写 --task
export ARIADNE_TASK_MODE_POLICY=auto   # off | on | auto
ariadne --task-mode-policy auto run "继续"
```

| 策略 | 行为 |
| --- | --- |
| `auto`（默认） | 普通 tool loop；除非 `--task` / API `task_mode=true`，或本会话已有 **active task**（自动恢复） |
| `on` | 始终 task 模式 |
| `off` | 除非本轮强制 `task_mode=true`，否则不用 task 模式 |

每轮会发事件 **`task_mode_resolved`**：`{enabled, reason, policy}`。

离线半集成：`tests/test_closed_loop_semi_e2e.py`。可选真模型：
`ARIADNE_LIVE_CLOSED_LOOP=1 uv run pytest tests/test_closed_loop_live.py`。  
以下能力默认 **关闭**（需显式环境变量打开）：

| 环境变量 | 默认 |
| --- | --- |
| `ARIADNE_ENABLE_SEMANTIC_VERIFIER` | off |
| `ARIADNE_ENABLE_CONTROLLED_DELEGATION` | off |
| `ARIADNE_ENABLE_MEMORY_PROJECTION` | off |

Web：`POST /api/turns`（及 stream）请求体字段 `task_mode: true`。
对话 UI 有 **任务模式** 勾选框，并根据 SSE 的 `task_mode_resolved` / `task_*`
事件显示状态条。

### Memory 预算（Phase 15，默认自动）

个人默认 **不必配置任何环境变量**。可选：

```bash
export ARIADNE_MEMORY_PROFILE=default   # compact | default | deep
# 单字段覆盖（先 profile，再 override）：
# ARIADNE_MEMORY_RECENT_LIMIT、ARIADNE_MEMORY_LAYER_BUDGETS（JSON）、
# ARIADNE_MEMORY_EPISODE_MAX_*、ARIADNE_MEMORY_CAPTURE_*
export ARIADNE_MEMORY_SCALE_TO_CONTEXT=0   # 1 = 按上下文缩放 recent/层预算
export ARIADNE_CONTEXT_MAX_CHARS=120000    # host 提示预算（缩放参考）
```

设计见 [MEMORY.md](../MEMORY.md)、
[design/memory-intelligence.md](../design/memory-intelligence.md)。

## 会话标题（主题总结）

每个会话可有 **标题**（短主题总结），存在 `data_dir/sessions/meta/<id>.json`。

| 操作 | CLI | Web |
| --- | --- | --- |
| 查看 | `/title` | 顶栏标题 + 侧栏列表 |
| 手动设置 | `/title 部署脚本` | 点顶栏标题，或双击侧栏会话 |
| 自动刷新 | `/title --refresh` | 对话后自动；改名时留空确定=强制重总结 |

- **auto**：由前几轮用户话启发式生成（不额外调 LLM）  
- **user**：手动标题不会被 auto 覆盖（除非 force）  
- 列表接口返回 `title`、`title_source`  
- `PATCH /api/sessions/{id}`：`{ "title": "…" }` 或 `{ "refresh_title": true }`

## 图片（CLI + Web）

在**模型支持视觉/多模态**时可粘贴或附加图片。

| 宿主 | 用法 |
| --- | --- |
| **CLI** | `/image`（剪贴板，需 `xclip`/`wl-paste`）或 `/image ./shot.png`；`/images` 查看待发送；`/clear-images` 清空。有附件时提示符显示 `[N img]`。 |
| **Web** | 在输入框 **Ctrl+V** 粘贴截图，或拖拽图片到输入区；上方 chip 预览。 |

策略环境变量：**`ARIADNE_VISION`**

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | 仅当模型名像常见 vision 模型时允许发图（如 `gpt-4o`、`claude-3`、`gemini`、`LongCat` 等） |
| `on` | 始终尝试多模态请求（上游仍可能拒绝） |
| `off` | 发图前一律拒绝 |

若带图且策略判定不支持，会 **fastfail**：错误码 **`ARIADNE_MULTIMODAL_UNSUPPORTED`** 与明确提示（不会静默丢图）。

## 交互元命令

```text
/help
/exit /quit
/status                     紧凑宿主状态
/mode [auto|on-request|readonly]
/session
/workspace
/tools
/skills
/model [name]               查看或热切换模型
/memory read                当前 working set 元数据 + curated
/usage                      本 REPL 累计 token
/compact                    归档 transcript（摘要仍保留历史）
/resume [id]                列出或切换会话
/new | /reset-session       新 session id，保留工作区
/sandbox-status
/clear-session-files
/clear
```

REPL 说明：历史保存在 data dir；`\` 续行与 \`\`\` 围栏支持多行输入；  
Ctrl+C 中断当前 turn（沙箱仍会清理）；空提示符再 Ctrl+C 退出。  
非 TTY 下裸 `ariadne` 不会挂死（打印帮助；若带了 prompt 则走单轮）。

## 文件工具

除 `sandbox_exec` 外，模型还可使用结构化文件工具：

```text
sandbox_read_file   {path}
sandbox_write_file  {path, content}                 → 统一 diff
sandbox_edit_file   {path, old_string, new_string}  → 精确一次匹配 + 统一 diff
```

`old_string` 匹配 0 次或多次时 fastfail；CLI 会对返回的 diff 做语法高亮。

## 插件（用户属性）

插件凭据归**用户**所有，而不是多公司 pack 体系。

| 宿主 | 默认存储 | 说明 |
| --- | --- | --- |
| CLI | `~/.ariadne/plugins.json`（权限 `0600`） | 跨工作区。`--workspace-scope` 写入项目 `data_dir/plugins.json`。Compose 合并顺序：**用户 → 工作区**（同名工作区优先）。 |
| Web | `data_dir/web/users/<username>/plugins.json` | 按注册账号隔离。API：`GET/PUT/DELETE /api/me/plugins`。Web **不**合并 home。已启用插件在**每次** Web turn 注册（含作坊主线/旁支；经 `plugins_dir` 读账号 store，不随作坊 `data_dir` 丢失）。 |

### 双宿主：CLI vs Web（身份与工作区）

| | CLI | Web |
| --- | --- | --- |
| **身份** | Linux 用户 | 注册账号 |
| **工作区** | 你打开的目录（`cwd` / `--workspace`） | 账号级 durable 看 **作坊** 主线/旁支；普通 **工作区** Tab = serve 主机目录（多会话共用，**不是**产品「项目」） |
| **Session** | 仅聊天线程，**不是**文件系统 | 同：对话 ≠ 文件树 |

无 serve 期 `project|per_user` 模式开关。Web 产品面：**对话 · 工作区 · 作坊**（无项目选择器）。

| Web 资源 | 范围 |
| --- | --- |
| `/workspace`（未进作坊） | serve 主机目录 — 同一 `serve` 上多会话/多账号共用 |
| `/workspace`（作坊内） | 主线树或旁支快照（当前会话可写） |
| `/main-readonly`（仅旁支） | 主线 `workspace/` **实时只读** |
| 对话 session | transcript + 标题；`/new` 不拷贝整树 |
| 作坊旁支 | 快照 + 独立记忆；读主线最新用 `/main-readonly` |
| 便签 | 作坊根 `KNOWLEDGE.md`（工具可读 `/workspace/KNOWLEDGE.md` 副本） |
| 记忆 / BYOK / 插件 / 作坊 | 按 Web 注册账号 |

规范设计：[design/web-workspace.md](../design/web-workspace.md)。

```bash
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token glpat-…
ariadne plugin disable gitlab
```

## Web UI

```bash
ariadne serve --port 8420
# → http://127.0.0.1:8420
```

典型流程：

1. 注册用户名 / 密码  
2. 在 **Provider** 中绑定 `BASE_URL`、`API_KEY`、`MODEL`  
3. 可选：在 **插件** 中启用官方插件  
4. 通过 SSE 流式对话  
5. **左侧栏**（类 Grok）：新对话、**对话**列表 · 工作区 · 作坊；底部 Provider / 插件 / 退出。  
   切换会话会加载该对话消息（`GET/POST/DELETE /api/sessions`、`GET /api/sessions/{id}`）  
6. **主题**：顶栏 ☀/☾ 或侧栏 **外观** 可切换浅色 / 深色；偏好写入
   `localStorage`（`ariadne_theme`），默认跟随系统  

浏览器端到端检查（Playwright）：

```bash
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  PYTHONPATH=src python3 scripts/verify_web.py
```

## 护栏与审批

- **入站 / 出站脱敏** — 常见 secret 模式在进入模型与 transcript 前脱敏，助手输出也会脱敏；类 prompt-injection 短语产生 `guard_finding` 警告（警告不硬阻断）。  
- **审批模式** — `--approval-mode auto`（默认）、`on-request`（变更类工具前确认）、`readonly`（拒绝写入）。拒绝结果映射为 `ARIADNE_TOOL_DENIED`，便于模型恢复。

## 工作原理

```text
你的提示
  → Agent / TurnApplication
  → task_mode_resolved（auto / --task / 进行中的 task）
  → 记忆层 + 技能索引 + ContextCompiler
  → 模型（OpenAI 兼容 tools；可选 stream）
  → /workspace 与 /session 上的沙箱工具
  → [task 模式] 校验步骤 done_when → 重规划 / needs_input / 完成
  → 可选 projection 投递 + 语义索引
  → 终端最终回答或 Web SSE（task 模式时含 TurnResult.task）
```

完整 CLI 设计见 [design/cli-shell-agent.md](../design/cli-shell-agent.md)（英文），  
产品介绍与截图见根目录 [README.zh-CN.md](../../README.zh-CN.md)。

## 排错

| 现象 | 检查 |
| --- | --- |
| 缺少 `BASE_URL` / `API_KEY` | `ariadne doctor`；`.env` 路径与变量名 |
| 模型返回 HTTP 401 | Key 是否有效；主机是否要求 `Authorization: Bearer …` |
| 极小烟测助手正文为空 | Provider「thinking」可能耗尽小 `max_tokens`；加大上限 |
| 代理搞挂本机测试 | 对本地假服务与 Playwright 取消 `HTTP_PROXY` / `HTTPS_PROXY` |
| 工作区被拒绝 | 默认拒绝 `/` 与 `$HOME`，需 `--force-workspace` |
