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
  <a href="#使用方式">使用</a> ·
  <a href="#架构">架构</a> ·
  <a href="#文档">文档</a> ·
  <a href="#非目标">非目标</a>
</p>

---

**Ariadne** 是一个可调用的运行时：把一次用户 turn 变成模型推理、技能引导、工具调用、分层记忆，以及可选的沙箱执行。首选形态是项目上的 **CLI 终端 Agent**，同时提供轻量 **Web UI** 与 Python API。

它**不是**企业多租户平台、连接器中枢或公司打包栈，而是那一层「难做对、又值得开源」的中间内核。

```text
你的提示
  → 组装记忆上下文
  → 技能发现 / 加载
  → 单一能力注册表 + 工具循环
  → 可选沙箱（/workspace · /session）
  → 持久化 transcript / 状态 / 结果
```

## 截图

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/cli-demo.jpg" alt="CLI 终端 Agent：文件工具与 diff" />
      <p align="center"><sub><b>CLI</b> — 单次 <code>run</code>、流式 chat、统一 diff</sub></p>
    </td>
    <td width="50%">
      <img src="docs/assets/web-demo.jpg" alt="Web UI：BYOK 与插件" />
      <p align="center"><sub><b>Web</b> — 注册、BYOK Provider、按用户插件</sub></p>
    </td>
  </tr>
</table>

## 为什么是 Ariadne

多数「Agent 框架」要么是薄薄的聊天壳 + 临时工具，要么是塞满连接器与部署包的公司平台。

| 你想要 | Ariadne 提供 |
| --- | --- |
| **可调用 Agent** | `await agent.run(...)` / CLI turn |
| **Skills** | 按需加载的过程性指导，而不是塞满每次 prompt |
| **Toolcall** | **唯一**能力注册表、延迟 schema、可审计循环 |
| **Memory** | 分层召回 + 精选事实 + 会话状态 |
| **Sandbox** | 可插拔执行（`local` / `docker` / `null`） |
| **宿主体验** | 终端 Agent + 可选 Web UI + 官方插件 |

## 功能特性

- **终端 Agent** — `run` / `chat`、流式、diff 高亮、审批模式、会话（`--continue`、`/resume`）
- **文件工具** — `sandbox_read_file` / `write` / `edit`，精确匹配失败 fastfail + 统一 diff
- **沙箱** — 工作区映射到 `/workspace`，临时区 `/session`，toolbox 配置与观察压缩
- **记忆 L0–L4** — transcript、摘要、精选事实、可选语义召回、L2 会话状态
- **Skills** — 文件系统 pack、混合检索、带分数的选择计划
- **护栏** — 入站 secret 脱敏 + 注入警告；出站脱敏
- **官方插件** — GitLab / Redmine / Odoo 作为**用户属性**（CLI 用户目录或 Web 账号）
- **Web UI** — `ariadne serve`，注册、BYOK `BASE_URL` / `API_KEY` / `MODEL`
- **OpenAI 兼容模型** — 任意支持 chat completions + tools 的端点

## 快速开始

### 1. 安装

```bash
git clone <your-fork-or-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
```

需要 **Python 3.13+**。不安装包时：

```bash
export PYTHONPATH=$PWD/src
```

### 2. 配置 OpenAI 兼容 LLM

复制示例环境文件，填入你的 Provider（任意兼容主机均可，如 LongCat、OpenAI、本地网关）：

```bash
cp .env.example .env
```

```bash
# .env — 切勿提交
BASE_URL=https://api.longcat.chat/openai/v1   # 或你的主机 .../v1
API_KEY=sk-...                                # Bearer token
MODEL=LongCat-2.0                             # 该主机上的模型 id
```

也会读取工作区 `.env` 与进程环境（`OPENAI_BASE_URL` / `OPENAI_API_KEY` 别名）。

### 3. 运行

```bash
# 健康检查
ariadne doctor

# 对当前目录（映射为 /workspace）跑一轮
ariadne run "create NOTES.md with a one-line outline of this project"

# 多轮 REPL（默认流式）
ariadne chat

# Web UI（注册用户，绑定自己的 Provider）
ariadne serve --host 127.0.0.1 --port 8420
```

离线测试（无需网络）：

```bash
PYTHONPATH=src python3 -m pytest -q
```

完整用法：**[docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md)**（中文）· **[docs/USAGE_CLI.md](docs/USAGE_CLI.md)**（English）。

## 使用方式

### CLI 速查

```bash
ariadne run "…"                 # 单轮
ariadne chat                    # REPL
ariadne --stream -v run "…"     # 流式 + 工具轨迹
ariadne -c chat                 # 继续最近会话
ariadne sessions                # 列出会话
ariadne tools / skills / toolbox
ariadne plugins
ariadne plugin enable gitlab --url https://gitlab.example.com --token glpat-…
ariadne plugin enable redmine --url … --api-key …
ariadne plugin enable odoo --url … --database … --login … --password …
# 项目级覆盖，而不是默认的 ~/.ariadne/plugins.json：
ariadne plugin enable gitlab --workspace-scope --url … --token …
```

常用 flags：`--workspace`、`--session`、`--sandbox local|docker|null`、`--approval-mode auto|on-request|readonly`、`--model`、`--json`。

### Python API（形态）

```python
from ariadne.config import load_settings
from ariadne.host.compose import compose_agent

agent = compose_agent(load_settings())
result = await agent.run("Summarize this repo and open a short TODO list")
print(result.text)
```

库向构造（`Memory.local`、`ToolRegistry`、`RunTurnCommand` 等）见 [docs/PUBLIC_API.md](docs/PUBLIC_API.md)（英文规范）。

### Web UI

```bash
ariadne serve --port 8420
# 打开 http://127.0.0.1:8420
```

每个注册用户有独立数据、BYOK Provider 与插件凭据（`/api/me/plugins`）。Playwright 冒烟：`scripts/verify_web.py`。

## 架构

```text
┌─────────────────────────────────────────────────────────────┐
│  宿主:  CLI (run/chat)  ·  Web (serve)  ·  库 Agent          │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  TurnApplication — 记忆组装 · 技能计划 · 工具循环            │
│       │                                                     │
│       ├─ Memory facade（L0 transcript … L2 state …）        │
│       ├─ SkillStore（索引 / 检索 / 加载）                    │
│       ├─ ToolRegistry（单一注册表，延迟暴露）                │
│       ├─ Model（OpenAI 兼容 chat + tools + stream）         │
│       └─ Sandbox port（local / docker / null）              │
└─────────────────────────────────────────────────────────────┘
```

**设计支柱**

1. **一个注册表** 管工具 — 绝不搞第二套临时工具系统  
2. **Skills ≠ tools** — 技能教；工具做  
3. **分层记忆** — 原始 turn、摘要、精选事实、可选语义 + 状态  
4. **延迟细节** — 先短目录，再按需拉全量 schema  
5. **Fastfail** — 非法 pack / 未知工具明确失败，不静默降级  
6. **Sandbox 是端口** — 执行环境可替换  
7. **个人优先** — 单用户友好；核心不做公司扩展模型  

## 文档

| 文档 | 说明 |
| --- | --- |
| [docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md) | **中文** CLI / Web / 插件用法 |
| [docs/USAGE_CLI.md](docs/USAGE_CLI.md) | **English** host usage |
| [docs/zh/README.md](docs/zh/README.md) | 中文文档索引 |
| [docs/README.md](docs/README.md) | 英文文档索引（含设计规范） |
| [docs/VISION.md](docs/VISION.md) 等 | 内核设计规范（英文，规范性文本） |

设计深读建议顺序：Vision → Principles → Architecture → Public API → Skills / Toolcall / Memory / Sandbox。

## 非目标

核心**明确不做**（设计如此）：

- Company Pack / 多公司部署模型  
- 企微 / 飞书 / Telegram / Slack 作为产品表面  
- 多租户 SaaS 控制面  
- 静默兼容回退  

**官方可选插件**（GitLab / Redmine / Odoo）以用户配置集成的方式支持，不是多公司 pack 体系。详见 [docs/NON_GOALS.md](docs/NON_GOALS.md)。

## 仓库结构

```text
src/ariadne/          内核、记忆、工具、技能、沙箱、CLI、Web
skills/builtin/       示例 skill packs
tests/                离线 pytest
docs/                 英文设计规范 + 用法
docs/zh/              中文用户文档
docs/assets/          README 图片
scripts/              llm_smoke.py、verify_web.py
```

## 名字

神话里 **Ariadne** 给了忒修斯逃出迷宫的线。  
这里迷宫是多步工具调用；线是 skills + 克制的工具暴露；记忆是你留下的东西，好让下一轮不再从零开始。

## 起源说明

Ariadne 重新诠释了私有生产 Agent 内核中已验证的想法（skills 运行时、延迟工具 schema、多层记忆）。它是**新的个人开源项目**，不是那套平台的公司打包、连接器或部署模型的 fork。

## 许可证

首个公开版本时再确定 License。当前文档与设计可供讨论使用。

---

<p align="center">
  <sub>为想要真正 Agent 内核的人而建 — 而不是又一个聊天壳。</sub>
</p>
