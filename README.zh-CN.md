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

**Ariadne** 是一个可调用的运行时：把一次用户 turn 变成模型推理、技能引导、工具调用、分层记忆，以及可选的沙箱执行。

默认人机界面：面向项目工作区的 **CLI 终端 Agent**（直接运行 `ariadne` 进入 REPL，对齐 codex）。  
可选：**Grok 风格 Web UI**（`ariadne serve`）——侧栏历史、深浅色主题、会话标题、图片粘贴。

它**不是**企业多租户平台、连接器中枢或公司打包栈。

```text
你的提示
  → 组装记忆上下文
  → 技能发现 / 加载
  → 单一能力注册表 + 工具循环
  → 可选沙箱（/workspace · /session）
  → 持久化 transcript / 状态 / 结果
```

## 截图

<p align="center">
  <img src="docs/assets/web-demo.jpg" alt="Ariadne Web 深色主题：侧栏会话与对话" width="920" />
</p>
<p align="center"><sub><b>Web 深色</b> — 左侧历史 + 新对话 · 会话主题标题 · Markdown 流式 · 底部输入框 · Provider / 插件 / 外观</sub></p>

<table>
  <tr>
    <td width="50%">
      <img src="docs/assets/web-demo-light.jpg" alt="Ariadne Web 浅色主题" />
      <p align="center"><sub><b>Web 浅色</b> — 同一壳层，顶栏切换浅色/深色</sub></p>
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
| **Sandbox** | 可插拔执行（`local` / `docker` / `null`） |
| **宿主体验** | 终端 Agent + Grok 风格 Web + 官方插件 |

## 功能特性

- **CLI 优先** — 裸 `ariadne` 进交互 REPL；`run` / `exec` 单轮；流式、diff、审批
- **会话** — continue / resume；**主题标题**（自动总结 + 手动 `/title` 或 Web 改名）
- **图片** — CLI `/image`（路径或剪贴板）；Web 粘贴/拖拽；非多模态模型明确报错（`ARIADNE_VISION`）
- **文件工具** — `sandbox_read_file` / `write` / `edit` + 统一 diff
- **记忆 L0–L4** — transcript、摘要、精选事实、语义召回、L2 会话状态
- **Skills** — pack、混合检索、带分数的选择计划（不倾倒全量 index）
- **护栏** — 入站/出站 secret 脱敏；注入警告
- **官方插件** — GitLab / Redmine / Odoo 为**用户属性**（密钥界面显示为 `***`）
- **Web UI** — 侧栏历史、BYOK Provider 弹窗、插件弹窗、浅色/深色主题
- **OpenAI 兼容模型** — chat completions + tools

## 宿主与界面

### CLI 终端（默认）

```bash
ariadne                 # 交互 REPL
ariadne "做一件事"       # REPL + 首轮
ariadne run "…"         # 非交互单轮
ariadne exec "…"        # run 别名
```

REPL 常用：`/help`、`/title`、`/image`、`/resume`、`/status`、`/mode`、`/exit`。

### Web UI

```bash
ariadne serve --host 127.0.0.1 --port 8420
# → http://127.0.0.1:8420
```

| 区域 | 与当前界面一致的行为 |
| --- | --- |
| **侧栏** | 新对话 · 带**主题标题**的历史 · Provider / 插件 / 外观 / 退出 |
| **主区** | 顶栏会话标题 · 模型 chip · Markdown 流式 · 工具 pill |
| **输入框** | 底部悬浮输入 · Enter 发送 · 粘贴/拖入图片 |
| **主题** | 浅色/深色切换（localStorage + 系统默认） |
| **会话** | 切换加载历史；双击行或点标题改名；自动主题总结 |

## 快速开始

### 1. 安装

```bash
git clone <your-fork-or-url>/Ariadne.git
cd Ariadne
python3 -m pip install -e ".[dev]"
```

需要 **Python 3.13+**。不安装时：

```bash
export PYTHONPATH=$PWD/src
```

### 2. 配置 OpenAI 兼容 LLM

```bash
cp .env.example .env
```

```bash
# .env — 切勿提交
BASE_URL=https://api.longcat.chat/openai/v1
API_KEY=sk-...
MODEL=LongCat-2.0
# 可选：ARIADNE_VISION=auto|on|off
```

### 3. 运行

```bash
ariadne doctor
ariadne
ariadne serve --port 8420
```

```bash
PYTHONPATH=src python3 -m pytest -q
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
ariadne plugins
ariadne plugin enable gitlab --url … --token …
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
┌─────────────────────────────────────────────────────────────┐
│  宿主:  CLI（默认 REPL）  ·  Web (serve)  ·  库 API           │
└────────────────────────────┬────────────────────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  TurnApplication — 记忆 · 技能计划 · 工具循环                 │
│       ├─ Memory facade                                      │
│       ├─ SkillStore（紧凑 SKILL_SELECTION + search/load）   │
│       ├─ ToolRegistry（单一注册表）                          │
│       ├─ Model（OpenAI 兼容 + 可选 vision）                  │
│       └─ Sandbox port                                       │
└─────────────────────────────────────────────────────────────┘
```

## 文档

| 文档 | 说明 |
| --- | --- |
| [README.md](README.md) | English overview |
| [docs/zh/USAGE_CLI.md](docs/zh/USAGE_CLI.md) | 中文宿主用法 |
| [docs/design/alignment-skills-toolcall-memory.md](docs/design/alignment-skills-toolcall-memory.md) | 设计对齐说明 |
| [docs/](docs/) | 英文设计规范索引 |

## 非目标

- Company Pack / 多公司部署模型  
- 企微 / 飞书 / Telegram / Slack 作为产品表面  
- 多租户 SaaS 控制面  
- 静默兼容回退  

官方插件（GitLab / Redmine / Odoo）是用户配置的集成，不是多公司 pack。见 [docs/NON_GOALS.md](docs/NON_GOALS.md)。

## 仓库结构

```text
src/ariadne/          内核、记忆、工具、技能、沙箱、CLI、Web
skills/builtin/       示例 skill packs
tests/                离线 pytest
docs/                 设计规范 + 用法
docs/zh/              中文用户文档
docs/assets/          README 配图（hero、CLI、Web 深/浅色）
scripts/              llm_smoke.py、verify_web.py
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
