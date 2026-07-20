# Ariadne 中文文档

> 语言：[English docs index](../README.md) · **简体中文**

面向用户的中文文档入口。内核**设计规范**（架构、记忆、工具契约等）以英文为规范性文本，列在下方「英文设计文档」中。

## 用户文档（中文）

| 文档 | 说明 |
| --- | --- |
| [../../README.zh-CN.md](../../README.zh-CN.md) | 项目介绍、截图、快速开始 |
| [USAGE_CLI.md](USAGE_CLI.md) | CLI / Web / 插件完整用法 |
| [../../README.md](../../README.md) | English product overview |
| [../USAGE_CLI.md](../USAGE_CLI.md) | English host usage |

## 英文设计文档（规范性）

下列文档为设计真源（English）。实现与评审以它们为准；中文 README/用法是用户向摘要。

| Doc | Topic |
| --- | --- |
| [../VISION.md](../VISION.md) | 愿景 |
| [../DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md) | 硬性原则 |
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | 内核结构与 turn 生命周期 |
| [../PUBLIC_API.md](../PUBLIC_API.md) | 可调用表面 |
| [../SKILLS.md](../SKILLS.md) | Skills 运行时 |
| [../TOOLCALL.md](../TOOLCALL.md) | 注册表与工具循环 |
| [../MEMORY.md](../MEMORY.md) | 分层记忆 |
| [../SANDBOX.md](../SANDBOX.md) | 沙箱端口 |
| [../ROADMAP.md](../ROADMAP.md) | 交付清单 |
| [../NON_GOALS.md](../NON_GOALS.md) | 明确不做 |
| [../GLOSSARY.md](../GLOSSARY.md) | 术语 |
| [../design/](../design/) | 深度设计稿 |

## 建议阅读顺序

**上手（中文）**

1. [README.zh-CN.md](../../README.zh-CN.md)  
2. [USAGE_CLI.md](USAGE_CLI.md)  
3. 需要时对照英文 [PUBLIC_API.md](../PUBLIC_API.md)

**改内核 / 做设计（英文）**

1. Vision → Design principles → Architecture  
2. Public API  
3. Skills → Toolcall → Memory → Sandbox  
4. Roadmap  

## 状态

**v0.2 usable** — `src/ariadne` 已实现内核 + CLI + Web + 插件，并有离线测试。  
产品介绍：[README.zh-CN.md](../../README.zh-CN.md) · 用法：[USAGE_CLI.md](USAGE_CLI.md)。

## 语言策略

| 类型 | 语言 | 位置 |
| --- | --- | --- |
| 产品介绍 | 中 / 英 | `README.zh-CN.md` · `README.md` |
| 宿主用法 | 中 / 英 | `docs/zh/USAGE_CLI.md` · `docs/USAGE_CLI.md` |
| 设计规范 | 英文（规范性） | `docs/*.md` · `docs/design/` |
| 文档索引 | 中 / 英 | `docs/zh/README.md` · `docs/README.md` |

欢迎 PR 补充更多中文用户文档；变更设计契约时请同步更新英文规范文档。
