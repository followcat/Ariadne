# Source Map

Ariadne is a **new personal open-source project**. It is not a git fork of AIFlow.

This document records which AIFlow branches, packages, and docs informed Ariadne’s design so contributors understand provenance and intentional omissions.

Survey date context: 2026-07-17, AIFlow core repository branches `skills`, `toolcall`, `memory`, and docs on `optimize`/`main`-line trees.

## 1. Branches consulted

| Branch | Tip (at survey) | What we took | What we left |
| --- | --- | --- | --- |
| `skills` | `f0b8253...` | skill catalog layout, loader/registry concepts, selection roadmap problems (index tax, keyword search ceiling), `search_skills`/`load_skill` split | company skill namespaces, platform-bound skill bindings, heavy enterprise catalog sync machinery as required core |
| `toolcall` | `a653183...` | unified capability registry, eager schema cost problem, deferred schema goals, case/scorecard discipline, tool exposure state idea | business tools, inspector-specific deployment lab coupling as product requirement |
| `memory` | `de8ed69...` | layered memory, curated vs episodic, turn summary, conversation state projection strictness, benchmark admission rules | multi-tenant ops tooling, company knowledge packs |
| `optimize` (docs) | various | sandbox portability lessons, tool vs CLI boundary, runtime contracts mindset | full OpenSandbox+egress+mail mesh, connector contracts |

## 2. Code areas that inspired kernel modules

| AIFlow area | Ariadne target module | Notes |
| --- | --- | --- |
| `src/aiflow_core/skills/models.py` (`CapabilitySpec`) | `ariadne.tools.models` | Keep catalog description vs tool_schema split |
| `src/aiflow_core/skills/registry.py` (`CapabilityRegistry`, `ToolExposureState`) | `ariadne.tools.registry` | Deferred load + tool_search modes |
| `src/aiflow_core/skills/loader.py` | `ariadne.tools.builtin` + skill-related tools | Reimplement cleanly; drop company credential matrices as core |
| `src/aiflow_core/skills/runtime.py` (`CapabilityRuntime`) | `ariadne.tools.runtime` | Dispatch only; strip ERP/mail-specific handlers |
| `src/aiflow_core/services/memory_service.py` (`MemoryContext`, `build_context`) | `ariadne.memory.context` | Layered context object is gold |
| curated / dream / turn_summary / conversation_state services | `ariadne.memory.layers.*` | Phased; state projection advanced |
| `src/aiflow_core/api/responses.py` tool loop | `ariadne.kernel.turn` | One turn application entry + loop limit |
| `src/aiflow_core/services/sandbox_runtime.py` + `sandbox/` | `ariadne.sandbox.port` | **Redesign**; keep idea of session/exec/close only |
| `skills/catalog/**` | example `skills/` | Re-author without company terms |

## 3. Documents that informed design

### Skills

| Source | Borrowed idea |
| --- | --- |
| `docs/agent-tools/skills-runtime-and-authoring.md` | three-layer skill runtime; catalog compiler mindset; validation rules |
| `docs/agent-tools/tool-definition-guidelines.md` | catalog vs schema vs policy layering |
| `docs/optimize/skills/selection-roadmap-design.md` | attention/token tax; vector search + ranking; load_body turn scope; prompt order |
| `docs/memory-evolution/02-procedural-memory-and-skill-manage-plan.md` | skills as procedural memory; versioning discipline |
| `docs/memory-evolution/06-background-skill-learning-loop.md` | optional later; not MVP |

### Toolcall

| Source | Borrowed idea |
| --- | --- |
| `optimize/toolcall/README.md` | evidence lab structure |
| `optimize/toolcall/GOALS.md` | deferred detail requirements; comparison rules |
| `optimize/toolcall/ARCHITECTURE_REVIEW.md` | single registry path; continuation schema resend problem |
| `optimize/toolcall/cases/*` | case taxonomy (stable_target, regression_control, schema_efficiency) |

### Memory

| Source | Borrowed idea |
| --- | --- |
| `docs/memory-evolution/README.md` + plans | evolution narrative, layered approach |
| `docs/memory-evolution/08-memory-v3-conversation-state.md` | projection strictness, no partial stale read |
| `optimize/memmory/GOALS.md` | baseline admission, scoring honesty |
| `optimize/memmory/ARCHITECTURE_REVIEW.md` | separate product FAIL vs infrastructure ERROR |
| turn summary / curated memory designs | compression + durable facts |

### Sandbox / runtime boundary

| Source | Borrowed idea |
| --- | --- |
| `docs/replatform-sandbox/01-runtime-architecture.md` | tool vs CLI natural form; sandbox as execution env |
| `docs/replatform-sandbox/14-sandbox-toolbox.md` | toolbox as optional profile, not kernel essence |
| `docs/replatform-sandbox/08-runtime-contracts.md` | contract mindset, explicit errors |

### Intentionally **not** imported into Ariadne core docs as features

| Source theme | Reason |
| --- | --- |
| `optimize/docs/split_structure/*` company pack model | user goal: no company extension system |
| connector development docs | connectors are hosts, not kernel |
| mail gateway / egress grant systems | enterprise mesh; optional host-level later |
| Odoo/GitLab/Redmine services | business adapters out of scope |

## 4. Concept translation table

| AIFlow concept | Ariadne concept |
| --- | --- |
| ResponseApplication / responses tool loop | `TurnApplication` / `Agent.run` |
| CapabilitySpec / CapabilityRegistry | same names (or ToolSpec alias) |
| Skill catalog + search/load tools | SkillStore + skill tools |
| MemoryContext layers | MemoryContext layers |
| SandboxRuntimeService | SandboxPort backends |
| Company Pack | **none** |
| Connector platform context | host `metadata` / host adapter only |
| Confirmation/grant control plane | optional host policy later |

## 5. Reimplementation policy

1. **Do not copy** proprietary company skills, credentials, or internal endpoints.
2. **Do not** vendor entire AIFlow trees into Ariadne.
3. Re-express ideas under Ariadne types and docs.
4. When a behavior is unclear, prefer Ariadne fastfail + smaller scope over compatibility with AIFlow enterprise semantics.
5. Attribution: high-level inspiration may be noted; code must be original to this repo unless intentionally extracted under a clear license review.

## 6. Working from AIFlow again later

If needed for more detail:

```bash
# on AIFlow core clone
git show skills:docs/agent-tools/skills-runtime-and-authoring.md
git show toolcall:optimize/toolcall/GOALS.md
git show memory:docs/memory-evolution/08-memory-v3-conversation-state.md
```

Ariadne docs remain the normative target; AIFlow docs are historical inputs.
