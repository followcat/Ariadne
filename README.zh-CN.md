<p align="center">
  <img src="docs/assets/hero.jpg" alt="Ariadne — 个人开源 Agent 内核" width="920" />
</p>

<h1 align="center">Ariadne</h1>

<p align="center">
  <strong>个人向开源 Agent 内核</strong><br/>
  Skills 是线 · Tools 是迷宫 · Memory 是你留下的地图
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <strong>简体中文</strong>
</p>

<p align="center">
  <a href="#快速开始"><img src="https://img.shields.io/badge/%E5%BF%AB%E9%80%9F%E5%BC%80%E5%A7%8B-5%20%E5%88%86%E9%92%9F-brightgreen?style=flat-square" alt="快速开始" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.13%2B-blue?style=flat-square" alt="Python 3.13+" /></a>
  <a href="docs/ROADMAP.md"><img src="https://img.shields.io/badge/status-v0.2%20usable-0e7-green?style=flat-square" alt="Status" /></a>
  <a href="docs/zh/"><img src="https://img.shields.io/badge/docs-%E4%B8%AD%E6%96%87%E7%94%A8%E6%B3%95-111827?style=flat-square" alt="中文文档" /></a>
  <a href="#许可证"><img src="https://img.shields.io/badge/license-TBD-lightgrey?style=flat-square" alt="License" /></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#功能特性">功能</a> ·
  <a href="#宿主与界面">宿主与界面</a> ·
  <a href="#使用方式">使用</a> ·
  <a href="#架构">架构</a> ·
  <a href="#文档">文档</a>
</p>

---

**Ariadne** 是一个可调用的运行时：把一次用户 turn 变成模型推理、技能引导、工具调用、分层记忆，以及 **Docker 隔离执行**（对标 Codex 的容器模型，跑在你本机）。

默认人机界面：面向项目工作区的 **CLI 终端 Agent**（直接运行 `ariadne` 进入 REPL）。  
可选：**Atelier 工坊**（`ariadne atelier` / Web **工坊** 页）与 **Grok 风格 Vue Web UI**（`ariadne serve`）——历史 · 对话 · 工具 · 工作区 · 项目说明。

它**不是**企业多租户平台、连接器中枢或公司打包栈。

```text
你的提示
  → 组装记忆上下文
  → 技能发现 / 加载
  → 单一能力注册表 + 工具循环
  → Docker 沙箱（/workspace 持久 · /session 临时 · 默认无网）
  → 持久化 transcript / 状态 / 结果
```

## 截图

<p align="center">
  <img src="docs/assets/web-demo.jpg" alt="Ariadne Web Vue 深色：侧栏、对话、工具面板" width="920" />
</p>
<p align="center"><sub><b>Web 深色</b> — 左侧历史 · 中间对话 · <b>右侧工具面板</b> · 底部输入框 · 主题 / 模型 chip</sub></p>

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/web-demo-light.jpg" alt="Ariadne Web 浅色主题（含工具面板）" />
      <p align="center"><sub><b>Web 浅色</b> — 同一三栏壳层 · 浅色主题</sub></p>
    </td>
    <td width="50%">
      <img src="docs/assets/cli-demo.jpg" alt="Ariadne CLI REPL" />
      <p align="center"><sub><b>CLI</b> — 裸 <code>ariadne</code> REPL · 工具与 diff · <code>/title</code> · <code>/image</code></sub></p>
    </td>
  </tr>
</table>

## 为什么是 Ariadne

多数「Agent 框架」要么是薄聊天壳，要么是塞满连接器的公司平台。

| 你想要 | Ariadne 提供 |
| --- | --- |
| **可调用 Agent** | `await agent.run(...)` / CLI turn |
| **Skills** | 按需过程性指导 + 紧凑选择计划 |
| **Toolcall** | **唯一**能力注册表、延迟 schema、可审计循环 |
| **Memory** | 分层召回 + 精选事实 + 会话状态 |
| **Sandbox** | **Docker 优先**加固容器（可选 `local` / `null`） |
| **Atelier** | 项目工坊：共享 workspace + `KNOWLEDGE.md` + 主/分支会话 |
| **宿主体验** | 终端 Agent + Vue Web UI + 工作区浏览器 + 插件 |

## 功能特性

- **CLI 优先** — 裸 `ariadne` 进交互 REPL；`run` / `exec` 单轮；流式、diff、审批
- **Docker 优先沙箱** — 默认 `ARIADNE_SANDBOX=docker`：cap-drop ALL、`--network none`、内存/CPU/pids 限制、非 root、只读根文件系统；官方镜像 `ariadne-sandbox:minimal`
- **语义化工具优先** — 优先 `sandbox_read_file` / `write` / `edit` / `list_dir`；`sandbox_exec` 为受策略约束的 shell 兜底；**`web_fetch` 在 host 执行**（外联白名单），容器默认无网
- **进程内 Runtime Agent** — 命令允许/拒绝 + 脱敏 + 审计 JSONL（非独立守护进程）
- **Atelier（工坊）** — `ariadne atelier`：共享代码树、`KNOWLEDGE.md`、主会话零管理；可选 **branch** 会话（对话隔离，**不是** git 分支）
- **会话** — continue / resume；**主题标题**（每轮自动总结 + `/title` 或点 Web 顶栏）
- **图片** — CLI `/image`；Web 粘贴/拖拽；非多模态明确报错（`ARIADNE_VISION`）
- **记忆 L0–L4** — transcript、摘要、精选事实、语义召回、L2 状态；可选巩固写入 L3
- **Skills** — pack、混合检索、分节加载、可选 discriminator、选择计划
- **护栏** — secret 脱敏；注入警告；on-request 审批可持久化 grant
- **官方插件** — GitLab / Redmine / Odoo 为**用户属性**（密钥显示为 `***`）
- **Web UI（Vue 3）** — 三栏：历史 · 对话 · 工具；**工作区浏览器**（project / per_user）；markdown-it 表格；thinking 折叠；回合统计；workspace 走势图内联
- **OpenAI 兼容模型** — chat completions + tools + 可选 reasoning 流

## 宿主与界面

### CLI 终端（默认）

```bash
ariadne                 # 交互 REPL
ariadne "做一件事"       # REPL + 首轮
ariadne run "…"         # 非交互单轮
ariadne exec "…"        # run 别名
```

REPL 常用：`/help`、`/title`、`/image`、`/resume`、`/status`、`/mode`、`/exit`。

### Atelier — 项目工坊

对标 Codex「打开项目」体验：共享代码、连续主会话、可选实验分支，以及手写的 `KNOWLEDGE.md`（类似 **AGENTS.md**）。

```text
Atelier = 工坊
├── workspace/       共享代码（所有 session 同一份）
├── KNOWLEDGE.md     项目说明 — 用户维护，始终注入
├── Main session     日常连续对话（零管理）
└── Branch session*  隔离对话 + 独立沙箱 scope
                     （不是 git 分支）
```

```bash
ariadne atelier create my-app --from .     # 从已有代码建工坊
ariadne atelier open my-app                # 进入 main REPL
ariadne atelier branch create my-app exp   # 实验会话
ariadne atelier branch merge my-app exp    # 附简短合并笔记 + 通知 main
ariadne atelier knowledge show my-app      # 编辑: knowledge edit
```

**项目说明（`KNOWLEDGE.md`）：** Codex 式——**你来写**稳定决策/约定；每轮注入有长度上限，并附带 workspace 文件树。  
**默认不做自动提取。** 分支共享代码、隔离对话；可注入主会话一行摘要作背景。  
模型空正文会兜底（思考摘要 / 工具提示），避免静默 `(empty reply)`。  
轮次记忆交给 **Memory L0–L4**。请保持说明文件精简。

**Web UI：** `ariadne serve` → **工坊** 页——创建/打开、主/分支会话、Markdown 编辑说明文件、`atelier_id` 对话。路径：`{data}/web/users/<user>/ateliers/`。

设计：[docs/design/atelier.md](docs/design/atelier.md)。
### Docker 沙箱（默认）

默认后端为 **Docker**（本机 Codex 式隔离）。无隔离逃生：

```bash
ariadne --sandbox local     # 主机目录，无隔离
ariadne --sandbox null      # 禁用 exec
```

```bash
# 构建官方 minimal 镜像（bash/git/curl + 非 root）
./scripts/build_sandbox_image.sh
# → ariadne-sandbox:minimal

ariadne doctor              # Docker / 镜像 / Provider
```

| 默认 | 值 |
| --- | --- |
| 网络 | `--network none`（HTTP 用 host `web_fetch` + 外联白名单） |
| 权限 | `--cap-drop ALL`、`no-new-privileges` |
| 资源 | 512m / 0.5 CPU / 128 PIDs（可按 profile 调） |
| 文件系统 | `/workspace` 挂项目 · `/session` 临时 · 可选只读根 |

详见：[docs/SANDBOX.md](docs/SANDBOX.md) · [docs/design/sandbox-v1.md](docs/design/sandbox-v1.md)。

### Web UI（Vue）

```bash
ariadne serve --host 127.0.0.1 --port 8420
# → http://127.0.0.1:8420
```

| 区域 | 行为 |
| --- | --- |
| **左侧栏** | **历史 \| 工作区** · 会话标题 · 外观 / Provider / 插件 |
| **工作区浏览器** | 浏览 `/workspace`（`project` 或 `per_user` 模式） |
| **主栏** | 顶栏 · 对话 · thinking 折叠 · 回合统计 |
| **右侧工具面板** | 可折叠 · 调用详情 · 耗时 / tokens / tool 数 |
| **Markdown** | markdown-it 表格 · workspace 图片内联（走势图） |

```bash
cd frontend && npm ci && npm run build:fast
cd frontend && npm run dev
```

## 快速开始

### 1. 前置

- **Python 3.13+**
- **Docker**（默认沙箱需要 daemon）  
  无 Docker 时：`ariadne --sandbox local`（无隔离）

### 2. 安装

```bash
git clone <your-fork-or-url>/Ariadne.git
cd Ariadne
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e ".[dev]"
./scripts/build_sandbox_image.sh
```

### 3. 配置 OpenAI 兼容 LLM

```bash
cp .env.example .env
```

```bash
# .env — 切勿提交
BASE_URL=https://api.longcat.chat/openai/v1
API_KEY=sk-...
MODEL=LongCat-2.0
# 可选：
# ARIADNE_SANDBOX=docker
# ARIADNE_SANDBOX_NETWORK=none
# ARIADNE_EGRESS_ALLOWED=api.github.com,example.com
# ARIADNE_VISION=auto|on|off
```

### 4. 运行

```bash
ariadne doctor
ariadne
ariadne atelier create demo --from .
ariadne serve --port 8420
```

```bash
python -m pytest -q
```

完整用法：**[docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md)** · **[docs/USAGE_CLI.md](docs/USAGE_CLI.md)**。

## 使用方式

### CLI 速查

```bash
ariadne
ariadne "create NOTES.md with a one-line outline"
ariadne run "summarize README.md"
ariadne -c
ariadne resume --last
ariadne sessions
ariadne doctor
ariadne plugins
ariadne plugin enable gitlab --url … --token …

# Atelier 工坊
ariadne atelier create my-app --from .
ariadne atelier open my-app
ariadne atelier branch create my-app jwt-vs-session
ariadne atelier branch merge my-app jwt-vs-session
ariadne atelier knowledge show my-app

# REPL 内：
#   /title 部署脚本     /title --refresh
#   /image ./shot.png   /image
#   /help /status /exit
```

### Python API（形态）

```python
from ariadne.config import load_settings
from ariadne.host.compose import compose_agent

agent = compose_agent(load_settings())
result = await agent.run("Summarize this repo and open a short TODO list")
print(result.text)
```

见 [docs/PUBLIC_API.md](docs/PUBLIC_API.md)。

## 架构

```text
┌──────────────────────────────────────────────────────────────────┐
│  宿主：CLI REPL · Atelier 工坊 · Web (serve) · 库 API             │
└──────────────────────────────┬───────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│  TurnApplication — 记忆 · 技能计划 · 工具循环                       │
│    ├─ Memory L0–L4 · 可选巩固 → L3                                │
│    ├─ SkillStore（选择计划 · 分节加载）                             │
│    ├─ ToolRegistry（语义文件工具 · web_fetch · shell）              │
│    ├─ RuntimeAgent（进程内策略 · 审计 · 外联）                      │
│    ├─ Model（OpenAI 兼容 + 可选 vision）                            │
│    └─ Sandbox port → Docker（默认）/ local / null                   │
│         加固容器 · /workspace · /session · 默认无网                 │
└──────────────────────────────────────────────────────────────────┘
```

设计支柱：单一注册表 · skills ≠ tools · 分层记忆 · 延迟细节 · fastfail · 沙箱端口 · **Docker 优先个人隔离** · **Atelier 工坊** 作为宿主 UX。

## 文档

| 文档 | 说明 |
| --- | --- |
| [README.md](README.md) | English overview |
| [docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md) | 中文宿主用法 |
| [docs/design/atelier.md](docs/design/atelier.md) | **Atelier 工坊**（main/branch + KNOWLEDGE） |
| [docs/design/sandbox-v1.md](docs/design/sandbox-v1.md) · [docs/SANDBOX.md](docs/SANDBOX.md) | **Docker 优先**沙箱 |
| [docs/design/web-workspace.md](docs/design/web-workspace.md) | Web 工作区模式（project / per_user） |
| [docs/design/web-vue-frontend.md](docs/design/web-vue-frontend.md) | Vue Web UI 与 markdown-it 栈 |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | 验收矩阵 → 测试 |
| [docs/](docs/) | 英文设计规范索引 |

## 非目标

- Company Pack / 多公司部署模型  
- 企微 / 飞书 / Telegram / Slack 作为产品表面  
- 多租户 SaaS 控制面  
- 静默兼容回退  

官方插件（GitLab / Redmine / Odoo）是用户配置的集成，不是多公司 pack。见 [docs/NON_GOALS.md](docs/NON_GOALS.md)。

## 仓库结构

```text
src/ariadne/                 内核、记忆、工具、技能、沙箱、atelier、CLI、Web
src/ariadne/atelier/         工坊（manager / knowledge / runner）
src/ariadne/sandbox/         Docker 优先端口 + RuntimeAgent + 策略
src/ariadne/web/static/dist/ Vue 生产构建
frontend/                    Vue 3 + Vite 源码
docker/sandbox/              官方 minimal 沙箱 Dockerfile
scripts/                     build_sandbox_image.sh、verify_web.py …
skills/builtin/              示例 skill packs
tests/                       离线 pytest
docs/design/atelier.md       Atelier 设计
docs/design/sandbox-v1.md    沙箱契约
docs/assets/                 README 配图
```

## 名字

神话里 **Ariadne** 给了忒修斯逃出迷宫的线。  
这里迷宫是多步工具调用；线是 skills + 克制的工具暴露；记忆是你留下的东西。

## 许可证

首个公开版本时再确定 License。当前文档与设计可供讨论使用。

---

<p align="center">
  <sub>为想要真正 Agent 内核的人而建 — 而不是又一个聊天壳。</sub>
</p>
