# Acceptance matrix (personal kernel)

Maps critical CLI / library / web paths to automated tests or scripts.  
Normative for “complete enough to trust” (calibrated roadmap Phase A).

Run: `pytest -q` from repo root. Optional web browser: `python scripts/verify_web.py`.

## Library (callable kernel)

| ID | Scenario | Coverage |
| --- | --- | --- |
| L1 | Turn with FakeModel + tool loop completes | `tests/test_turn_e2e_fake_model.py` |
| L2 | Public API types / Memory.local helpers | `tests/test_public_api.py` |
| L3 | Tool denied by approval becomes structured error, not turn crash | `tests/test_approval.py` |
| L4 | Deferred tools + exposure plan | `tests/test_tool_exposure.py` |
| L5 | Sandbox FS path confinement + lifecycle | `tests/test_sandbox_acceptance.py`, `tests/test_local_sandbox.py` |
| L6 | Memory layers, isolation, CAS | `tests/test_memory_acceptance.py`, `tests/test_memory_layers.py` |
| L7 | Skill load / search / hybrid | `tests/test_skills_store.py`, `tests/test_hybrid_search.py` |
| L8 | Skill body **section** load + discriminator metadata | `tests/test_skill_section_and_discriminator.py` |
| L9 | Memory **consolidation** → L3 curated (explicit apply) | `tests/test_memory_consolidation.py` |
| L10 | **Persistent grants** survive reload | `tests/test_grants.py` |

## CLI host

| ID | Scenario | Coverage |
| --- | --- | --- |
| A1 | Atelier create/list + shared workspace + branch merge/discard | `tests/test_atelier_manager.py` |
| A2 | Atelier knowledge template/heuristic/history | `tests/test_atelier_knowledge.py` |
| A3 | Atelier prompt inject + settings bind | `tests/test_atelier_runner.py` |
| C1 | Parser: bare entry, subcommands, flags | `tests/test_cli_parser.py` |
| C2 | Sessions list/continue helpers | `tests/test_sessions.py` |
| C3 | Approval modes auto / on-request / readonly | `tests/test_approval.py` |
| C4 | Memory worker drain + consolidate flag | `tests/test_memory_consolidation.py` (CLI surface via module), worker unit paths |
| C5 | Grant store used by on-request approval | `tests/test_grants.py` |

## Web host

| ID | Scenario | Coverage |
| --- | --- | --- |
| W1 | Register / login / me | `tests/test_web_api.py::test_register_login_me` |
| W2 | Provider BYOK binding | `tests/test_web_api.py::test_provider_binding` |
| W3 | Sessions CRUD + title | `tests/test_web_api.py::test_sessions_api` |
| W4 | Plugins per user | `tests/test_web_api.py::test_per_user_plugins` |
| W5 | Workspace list/read/file + host path | `tests/test_web_api.py::test_workspace_browse_api` |
| W6 | Workspace mode `per_user` isolation | `tests/test_web_api.py::test_web_workspace_mode_per_user` |
| W7 | SPA index served | `tests/test_web_api.py::test_index_served` |
| W8 | Optional live browser smoke | `scripts/verify_web.py` (env-dependent) |

## Design notes

- Workspace vs session vs account: [design/web-workspace.md](design/web-workspace.md)
- Skills / toolcall / memory alignment: [design/alignment-skills-toolcall-memory.md](design/alignment-skills-toolcall-memory.md)
