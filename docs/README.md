# Ariadne Docs

> Language: **English** · [简体中文索引](zh/README.md)

Normative design documents for **Ariadne** (筑梦师) — personal open-source agent kernel.  
User-facing pages are bilingual; design contracts stay **English-first** (see [I18N.md](I18N.md)).

## User docs (EN · 中文)

| Doc | Topic |
| --- | --- |
| [../README.md](../README.md) · [../README.zh-CN.md](../README.zh-CN.md) | Product overview (EN · 中文) |
| [USAGE_CLI.md](USAGE_CLI.md) · [zh/USAGE_CLI.md](zh/USAGE_CLI.md) | CLI / web / plugins usage |
| [zh/README.md](zh/README.md) | Chinese docs hub |
| [I18N.md](I18N.md) | Bilingual documentation policy |

## Design docs (English, normative)

| Doc | Topic |
| --- | --- |
| [VISION.md](VISION.md) | Why Ariadne exists |
| [DESIGN_PRINCIPLES.md](DESIGN_PRINCIPLES.md) | Hard rules |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Kernel structure & turn lifecycle |
| [PUBLIC_API.md](PUBLIC_API.md) | Callable surface |
| [SKILLS.md](SKILLS.md) | Skills runtime & authoring |
| [TOOLCALL.md](TOOLCALL.md) | Registry, exposure, tool loop |
| [MEMORY.md](MEMORY.md) | Layered memory |
| [SANDBOX.md](SANDBOX.md) | Redesignable execution port |
| [ROADMAP.md](ROADMAP.md) | Phased delivery |
| [SOURCE_MAP.md](SOURCE_MAP.md) | Provenance from AIFlow branches/docs |
| [NON_GOALS.md](NON_GOALS.md) | Explicit exclusions |
| [GLOSSARY.md](GLOSSARY.md) | Terms |
| [design/memory-v1.md](design/memory-v1.md) | Improved memory architecture |
| [design/memory-scopes.md](design/memory-scopes.md) | User / workspace / session memory scopes |
| [design/memory-search.md](design/memory-search.md) | Graded `memory_search` (fast/auto/deep) |
| [design/agent-closed-loop.md](design/agent-closed-loop.md) | Plan → act → verify → replan (next quality leap) |
| [design/sandbox-v1.md](design/sandbox-v1.md) | Redesigned sandbox port |
| [design/memory-sandbox-synthesis.md](design/memory-sandbox-synthesis.md) | How memory and sandbox meet |
| [design/cli-shell-agent.md](design/cli-shell-agent.md) | CLI as primary shell-agent host |

## Reading order

**Getting started:** README → USAGE_CLI (pick your language).

**Design deep-dives:**

1. Vision  
2. Design principles  
3. Architecture  
4. Public API  
5. Skills → Toolcall → Memory → Sandbox  
6. Roadmap  
7. Source map (if you care about provenance)

## Status

**v0.2 usable** — kernel + CLI + web + plugins implemented under `src/ariadne`
with offline tests. Product overview: [../README.md](../README.md) /
[../README.zh-CN.md](../README.zh-CN.md). Host usage: [USAGE_CLI.md](USAGE_CLI.md) /
[zh/USAGE_CLI.md](zh/USAGE_CLI.md). Delivery checklist: [ROADMAP.md](ROADMAP.md).
