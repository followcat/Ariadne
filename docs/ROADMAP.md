# Roadmap

## Implemented (v0.1 kernel)

The personal open-source kernel designs are implemented in `src/ariadne` with offline tests.

| Design area | Status | Code | Tests |
| --- | --- | --- | --- |
| CLI shell agent host | done | `cli/`, `host/compose.py` | `test_cli_parser.py` |
| Callable turn / tool loop | done | `kernel/turn.py`, `agent.py` | `test_turn_e2e_fake_model.py` |
| Sandbox port + LocalWorkdir | done | `sandbox/` | `test_local_sandbox.py` |
| Tool registry + deferred exposure | done | `tools/` | `test_tool_exposure.py` |
| Skills store/search/load | done | `skills/` | `test_skills_store.py` + e2e |
| Memory L0 transcript | done | `memory/transcript.py` | e2e |
| Memory L1 turn summary store | done | `memory/summary.py` | e2e |
| Memory L2 conversation state | done | `memory/state.py` | `test_memory_layers.py` |
| Memory L3 curated durable | done | `memory/curated.py` | `test_memory_layers.py` |
| Memory L4 semantic index | done | `memory/semantic.py` | e2e |
| Memory facade / layer reports | done | `memory/facade.py` | `test_memory_layers.py` |
| OpenAI-compatible model adapter | done | `model/openai_chat.py` | live via `.env` / CLI |
| FakeModel for offline verification | done | `model/fake.py` | e2e |

### Phase 0 — Docs & repo skeleton
- [x] Name: Ariadne
- [x] Git repository + design docs
- [x] Package skeleton (`pyproject.toml`)

### Phase 0.5 / 1 — CLI + callable turn
- [x] `ariadne run` / `chat` / `doctor` / `tools` / `skills`
- [x] LocalWorkdir sandbox + `sandbox_exec`
- [x] OpenAI-compatible model via `.env`
- [x] L0 transcript under `.ariadne/sessions/`
- [x] Full tool loop with structured errors
- [ ] Streaming tokens in CLI
- [ ] `active_session` sandbox for chat

### Phase 2 — Skills runtime
- [x] Filesystem skill packs + validation
- [x] Skill index injection
- [x] `search_skills` (lexical) + `load_skill` (turn-scoped tool result)
- [x] Example builtin skill `shell_project_notes`
- [ ] Hybrid/vector skill search

### Phase 3 — Toolcall efficiency
- [x] Deferred exposure + `tool_search`
- [x] Catalog vs schema layering on builtins
- [x] Deferred demo tool + unit test
- [ ] Schema size metrics in production traces

### Phase 4 — Memory depth
- [x] Curated memory tool + store + caps/fastfail
- [x] Async-ready turn summary store (written at turn end)
- [x] Semantic multi-chunk lexical index
- [x] Layer budgets/metadata via `LayerReport`
- [x] Conversation state closed ops + evidence quotes

### Phase 5 — Sandbox redesign v1
- [x] `NullSandbox` + clear errors
- [x] `LocalWorkdirSandbox` + truncation markers
- [x] `/workspace` + `/session` contract
- [ ] Docker backend
- [ ] Observation compression beyond head/tail
- [ ] Toolbox profiles

### Phase 6 — Advanced memory (optional)
- [ ] Background projection worker / leases
- [ ] External embedding providers
- [ ] Full memory lab case suite port

### Phase 7 — Polish for public 0.1
- [x] Offline e2e verification test
- [ ] CI packaging
- [ ] Streaming + richer CLI UX

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
