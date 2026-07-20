# CLI 与宿主用法

> 语言：[English](../USAGE_CLI.md) · **简体中文**

Ariadne 的主宿主是面向项目工作区的 **CLI 终端 Agent**。  
可选宿主：**Web UI**（`ariadne serve`）与 **Python** `Agent` API。

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
# 单轮（cwd → /workspace）
ariadne run "create NOTES.md with a one-line outline of this project"

# 多轮交互（默认 sandbox 生命周期：active_session）
ariadne chat

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

# Web UI — 注册用户，每人绑定自己的 Provider（BYOK）
ariadne serve --host 127.0.0.1 --port 8420

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
--sandbox-prestart            与 memory build 并行预热沙箱
--approval-mode MODE          auto | on-request | readonly 工具审批
-c / --continue               继续最近一次会话
-v / --verbose                工具轨迹、用量、schema 指标
--json                        输出 TurnResult JSON（含 schema_metrics）；
                              配合 --stream 时先 NDJSON 事件再最终结果
```

## Chat 元命令

```text
/help
/exit /quit
/session
/workspace
/tools
/skills
/model [name]               查看或热切换模型
/memory read
/usage                      本 REPL 累计 token
/compact                    归档 transcript（摘要仍保留历史）
/resume [id]                列出或切换会话
/reset-session              新 session id，保留工作区
/sandbox-status
/clear-session-files
/clear
```

REPL 说明：历史保存在 data dir；`\` 续行与 \`\`\` 围栏支持多行输入；  
Ctrl+C 中断当前 turn（沙箱仍会清理）；空提示符再 Ctrl+C 退出。

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
| Web | `data_dir/web/users/<username>/plugins.json` | 按注册账号隔离。API：`GET/PUT/DELETE /api/me/plugins`。Web **不**合并 home。 |

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
  → 记忆层 + 技能索引
  → 模型（OpenAI 兼容 tools；可选 stream）
  → /workspace 与 /session 上的沙箱工具
  → 投递 projection 任务 + 语义索引
  → 终端最终回答或 Web SSE
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
