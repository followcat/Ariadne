# Roadmap

## Implemented (design coverage)

The personal open-source kernel designs are implemented in `src/ariadne` with offline tests.

| Design area | Status | Code | Tests |
| --- | --- | --- | --- |
| CLI shell agent host | done | `cli/`, `host/compose.py` | `test_cli_parser.py` |
| Streaming tokens / turn events | done | `model/*`, `kernel/turn.py`, CLI `--stream` | `test_turn_e2e_fake_model.py` |
| Callable turn / tool loop | done | `kernel/turn.py`, `agent.py` | `test_turn_e2e_fake_model.py` |
| Sandbox port + LocalWorkdir | done | `sandbox/` | `test_local_sandbox.py` |
| Sandbox FS API (`read_file`/`write_file`/`list_dir`) | done | `sandbox/port.py`, `local.py`, `docker.py`, `null.py` | `test_sandbox_acceptance.py` |
| Sandbox acceptance scenarios 1–6 | done | — | `test_sandbox_acceptance.py` |
| Public API types (`RunTurnCommand`, `TurnResult.messages`) | done | `types.py`, `kernel/turn.py`, `agent.py` | `test_public_api.py` |
| Memory constructors (`Memory.local`/`in_memory`) + inspection helpers | done | `memory/facade.py`, `agent.py` | `test_public_api.py` |
| `last_good_plus_delta` read mode | done | `memory/facade.py`, `state.py`, `transcript.py` | `test_public_api.py` |
| Sandbox prestart (parallel with memory build) | done | `kernel/turn.py`, `config.py` | `test_public_api.py` |
| Sandbox cleanup guard (all exit paths) | done | `kernel/turn.py` `_SandboxGuard` | `test_turn_lifecycle.py` |
| L2 append-only versions + CAS | done | `memory/state.py` | `test_memory_acceptance.py` |
| Full L2 op set (relations, collection_move) | done | `memory/state.py` | `test_memory_acceptance.py` |
| Layer char budgets with markers | done | `memory/facade.py` | `test_memory_acceptance.py` |
| Sandbox env allowlist | done | `sandbox/local.py` | `test_sandbox_acceptance.py` |
| CapabilitySpec fields + `ToolRegistry.builtins()` | done | `tools/registry.py`, `tools/exposure.py` | e2e |
| Trace secret redaction | done | `redact.py`, `kernel/turn.py` | `test_skill_plan_and_redact.py` |
| Skill selection plan + scores | done | `skills/store.py`, `kernel/turn.py` | `test_skill_plan_and_redact.py` |
| Skill pack allowlist + agents yaml | done | `skills/store.py` | `test_skill_plan_and_redact.py` |
| CLI hardening (flag order, /memory read, /reset-session, guards, validate, NDJSON) | done | `cli/main.py`, `config.py` | parser tests + live |
| Memory acceptance suite (isolation, forget/update, multi-entity) | done | — | `test_memory_acceptance.py` |
| Active session lifecycle | done | `sandbox/active.py` | `test_active_session.py` |
| Docker sandbox backend | done | `sandbox/docker.py` | `test_docker_sandbox.py` (skip if no docker) |
| Observation compression | done | `sandbox/compress.py` | `test_local_sandbox.py` |
| Toolbox profiles | done | `sandbox/toolbox.py` | `test_toolbox.py` |
| Tool registry + deferred exposure | done | `tools/` | `test_tool_exposure.py` |
| Schema size metrics | done | `types.SchemaMetrics`, turn traces | e2e |
| Skills store/search/load/manage | done | `skills/` | `test_skills_store.py` + hybrid |
| Hybrid/vector skill search | done | `skills/store.py` | `test_hybrid_search.py` |
| Memory L0–L4 + facade | done | `memory/` | `test_memory_layers.py` |
| Projection worker / leases | done | `memory/projection.py` | `test_projection_worker.py` |
| Embedding providers | done | `memory/embeddings.py` | hybrid tests (hash) + OpenAI provider |
| Memory lab cases | done | — | `test_memory_lab_cases.py` |
| OpenAI-compatible model + stream | done | `model/openai_chat.py` | live via `.env` / CLI |
| FakeModel offline verification | done | `model/fake.py` | e2e + stream |
| CI packaging | done | `.github/workflows/ci.yml` | — |

### Phase 0 — Docs & repo skeleton
- [x] Name: Ariadne
- [x] Git repository + design docs
- [x] Package skeleton (`pyproject.toml`)

### Phase 0.5 / 1 — CLI + callable turn
- [x] `ariadne run` / `chat` / `doctor` / `tools` / `skills` / `toolbox`
- [x] LocalWorkdir sandbox + `sandbox_exec`
- [x] OpenAI-compatible model via `.env`
- [x] L0 transcript under `.ariadne/sessions/`
- [x] Full tool loop with structured errors
- [x] Streaming tokens in CLI (`--stream`)
- [x] `active_session` sandbox for chat

### Phase 2 — Skills runtime
- [x] Filesystem skill packs + validation
- [x] Skill index injection
- [x] `search_skills` (lexical|hybrid) + `load_skill`
- [x] `skill_manage` for user skills
- [x] Example builtin skill `shell_project_notes`
- [x] Hybrid/vector skill search
- [x] Selection plan (auto_load / recommended / other) with explainable scores
- [x] Pack allowlist validation + agents/index.yaml + runtime.yaml

### Phase 3 — Toolcall efficiency
- [x] Deferred exposure + `tool_search`
- [x] Catalog vs schema layering on builtins
- [x] Deferred demo tool + unit test
- [x] Schema size metrics in production traces
- [x] CapabilitySpec fields (title/kind/exposed_to_llm) + ToolRegistry.builtins()
- [x] Secret redaction in tool traces

### Phase 4 — Memory depth
- [x] Curated memory tool + store + caps/fastfail
- [x] Async-ready turn summary store (written at turn end)
- [x] Semantic multi-chunk index (lexical + hybrid embeddings)
- [x] Layer budgets/metadata via `LayerReport`
- [x] Conversation state closed ops + evidence quotes

### Phase 5 — Sandbox redesign v1
- [x] `NullSandbox` + clear errors
- [x] `LocalWorkdirSandbox` + truncation markers
- [x] `/workspace` + `/session` contract
- [x] Docker backend
- [x] Observation compression beyond head/tail
- [x] Toolbox profiles
- [x] Session FS API (`read_file`/`write_file`/`list_dir`) + path-escape rejection
- [x] per_turn `/session` cleanup on close + acceptance scenarios 1–6
- [x] Optional sandbox prestart (bounded, parallel with memory build)
- [x] env allowlist (no host secrets forwarded by default)
- [x] cleanup guard: sandbox closed on every turn exit path

Model-facing `sandbox.read_file`/`sandbox.write_file` tools are intentionally
**not** added yet: sandbox-v1 §5.2 makes them conditional ("only if exec-based
base64 loops prove too error-prone"). The session-level FS API exists for
hosts and kernel code.

### Phase 6 — Advanced memory
- [x] Background projection worker / leases
- [x] External embedding providers (hash + OpenAI-compatible)
- [x] Memory lab case suite (lightweight port)
- [x] `last_good_plus_delta` read mode (last-good state + newer raw delta, `stale_delta` report)
- [x] Append-only state versions + CAS parent check
- [x] Full closed op set (relations + collection_move)
- [x] Per-layer char budgets with explicit truncation markers
- [x] Memory acceptance suite (isolation, forget/update, multi-entity, CAS)

### Phase 7 — Polish for public 0.1
- [x] Offline e2e verification test
- [x] CI packaging
- [x] Streaming + richer CLI UX

## Design references

- Memory deep design: [design/memory-v1.md](design/memory-v1.md)
- Sandbox deep design: [design/sandbox-v1.md](design/sandbox-v1.md)
- CLI shell agent: [design/cli-shell-agent.md](design/cli-shell-agent.md)
- Joint synthesis: [design/memory-sandbox-synthesis.md](design/memory-sandbox-synthesis.md)

## Explicitly deferred forever (unless product changes)

- Company Pack system
- WeCom/Feishu/Telegram connectors in core
- Odoo/GitLab/Redmine adapters in core
- enterprise mail/egress gateway mesh as required runtime
