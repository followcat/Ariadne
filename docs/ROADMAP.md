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
| rich terminal UX (REPL, streaming, history, spinner) | done | `cli/ui.py`, `cli/repl.py` | live + parser tests |
| File editing tools with diffs | done | `tools/filetools.py` | `test_filetools.py` |
| Tool approval modes | done | `cli/approval.py`, `tools/registry.py` | `test_approval.py` |
| Session management (sessions, --continue, /resume) | done | `cli/sessions.py`, `cli/repl.py` | `test_sessions.py` |
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

### Phase 8 — Terminal agent experience (codex/claude-code class)
- [x] rich rendering (markdown, styled tool blocks, diff syntax, spinner)
- [x] REPL: persistent history, multiline, Ctrl+C turn cancel, session:model prompt
- [x] chat streams by default; no-delta provider fallback
- [x] `sandbox_read_file` / `sandbox_write_file` / `sandbox_edit_file` + diffs
- [x] `--approval-mode auto|on-request|readonly` (host policy, kernel hook)
- [x] `ariadne sessions`, `--continue`, `/resume`, `/model`, `/usage`, `/compact`, `/clear`

## Design references

- Memory deep design: [design/memory-v1.md](design/memory-v1.md)
- Sandbox deep design: [design/sandbox-v1.md](design/sandbox-v1.md)
- CLI shell agent: [design/cli-shell-agent.md](design/cli-shell-agent.md)
- Joint synthesis: [design/memory-sandbox-synthesis.md](design/memory-sandbox-synthesis.md)

### Phase 9 — Guardrails, plugins, web UI
- [x] in/out bound guardrails (secret redaction + injection warnings)
- [x] Official plugins: odoo / gitlab / redmine with per-host credentials
- [x] `ariadne serve`: web UI, user registration, BYOK provider binding
- [x] SSE streaming turns in browser; per-user data isolation
- [x] Dual-host identity (CLI Linux vs Web accounts); Web 作坊 for per-account files;
  no product 「项目」 / mode switch; [design/web-workspace.md](design/web-workspace.md)
- [x] playwright end-to-end verification (`scripts/verify_web.py`)
- [x] in/out bound: secret redaction + injection warnings (`guardrails.py`)
- [x] Official plugin mechanism (odoo/gitlab/redmine, token-configured)
- [x] `ariadne plugins` / `ariadne plugin enable|disable` + 0600 credential store
- [x] Plugin configs as **user attributes**: CLI `~/.ariadne/plugins.json` (default)
  with optional `--workspace-scope`; Web per-account store + Settings UI
  (`GET/PUT/DELETE /api/me/plugins`); compose merges user → workspace

### Phase 10 — Default CLI mode (codex-style entry)
- [x] Bare `ariadne` → interactive REPL (`active_session`, stream on)
- [x] Optional argv prompt seeds the first REPL turn
- [x] `exec` alias of `run`; `chat` remains explicit interactive alias
- [x] `resume` subcommand (`--last` / session id)
- [x] Slash: `/status`, `/mode`, `/new`, grouped `/help`
- [x] Non-TTY bare entry does not hang
- [x] Docs EN+ZH + design/cli-shell-agent updated

### Phase 11 — Personal kernel completeness (calibrated A–C)
- [x] Acceptance matrix: [ACCEPTANCE.md](ACCEPTANCE.md)
- [x] Skill body `section=` load + optional discriminator frontmatter
- [x] Memory consolidation (dry-run / apply) → L3 curated;
  `ariadne memory-worker --consolidate [--apply]`
- [x] Persistent approval grants (`data_dir/grants.json`) for on-request mode

### Phase 11b — Memory scopes + graded search (personal 2C)
Design: [design/memory-scopes.md](design/memory-scopes.md),
[design/memory-search.md](design/memory-search.md) (linked from [MEMORY.md](MEMORY.md)).

**Status: S0–S2 complete; S3/S4 partial** (personal / single-host scale).

Shipped:

- [x] Scopes **user / workspace / session** for curated; stable UUID ids;
  `source_turn_id` + `source_session_id` (migration fills empty fields)
- [x] Host layout: CLI `~/.ariadne/memory`; Web
  `{account}/memory` via `user_id` + `user_memory_dir` (no cross-account share)
- [x] `user_id` mismatch **fastfail** (provided id must match facade bind)
- [x] `memory_search` tool (`scope`, `mode`, `limit`, `before_turn_id`);
  hard `limit` cap validation; grounded `turn_id` + `session_id`
- [x] Honest L2: projection off by default; summary input widen; skill pins
- [x] Chunk clocks `ts`/`seq`; as-of via `before_ts` on episodic indexes
- [x] Curated as-of: `source_turn` clock **and** entry `updated_at` strictly
  before cutoff (post-cutoff edits on old source turns do not leak)
- [x] User episodic index (`user_memory_dir/episodic/`); dual-write on turn
  complete; `scope=user` hybrid + provenance-bearing curated
- [x] Shared JSON stores: fcntl-locked RMW for **semantic** and **curated**
- [x] Embeddings: default **hash**; `openai` / `auto` opt-in only; unknown
  provider fastfail; empty corpus skips query embed; OpenAI via
  `asyncio.to_thread`; writeback keyed by text+seq (no stale vector on reindex)
- [x] Deep: `LocalSplitPlanner` + optional `ARIADNE_MEMORY_DEEP_PLANNER=llm`;
  two-phase **plan → subquery merge → rerank(final candidates)**; also
  **rerank-only** when decomp is empty; `mode_used=deep` only when candidates
  or order actually change; rerank failure keeps deep if decomp already
  changed results (`deep:rerank_failed` + score-order fallback notes)
- [x] Hit evidence: `source` ∈ `raw|summary|chunk|curated`; curated keeps
  `entry_id` + scope
- [x] Tests: `test_memory_scopes_search`, `test_memory_2c_design_gaps`,
  `test_memory_2c_regressions`

Still open / partial:

- [~] **S3 quality:** multi-hop recall depends on host LLM planner quality;
  no separate small-model SKU
- [~] **S4 completeness:** no historical backfill of turns before dual-write;
  no curated version history (as-of sees *current* entry or drop, not past
  content); no atelier branch lifecycle purge of user episodic; no multi-device
  sync

### Phase 12 — Docker-first hardened sandbox (Codex-aligned)
- [x] Default `sandbox=docker`; `local`/`null` explicit escape; doctor checks
- [x] Hardened `docker run` (cap-drop, network none, mem/cpu/pids, read-only rootfs)
- [x] Official minimal image `docker/sandbox/Dockerfile` + build script
- [x] In-process RuntimeAgent (command policy + audit); host `web_fetch` + egress allowlist
- [x] Semantic file tools preferred over shell in prompts/descriptions

### Phase 13 — Atelier（工坊 / 小作坊）
- [x] Design: [design/atelier.md](design/atelier.md) — naming + isolation
- [x] `ariadne atelier create|list|open|delete` + branch create/list/merge/discard
- [x] **Main workspace vs isolated branch sandboxes** (file snapshot + scopes/)
- [x] Web 作坊 UI：`atelier_id` / `atelier_session` turns + workspace browse + images
- [x] `KNOWLEDGE.md` 便签: user edit + main post-turn constrained 约定 auto-append;
  branch read-only; history snapshots
- [x] Delivery: empty-reply recovery, thrash cap, max_tokens 8k / atelier ≥16k
- [x] Tests: `test_atelier_*` + web atelier API

### Phase 14 — Closed-loop execution (plan → act → verify → replan)
Design: [design/agent-closed-loop.md](design/agent-closed-loop.md).

**North star:** after each material action, verify with evidence; update the plan
from evidence — not from “tool returned success.”

**Status: functional vertical slice complete; default-path hardening in
progress (not ToB production-ready).**

Checkboxes mean offline-tested kernel capability, **not** “always-on by
default” or multi-tenant readiness. Defaults (2C-safe):

| Knob | Default | Notes |
| --- | --- | --- |
| Task mode policy | `auto` | Direct loop unless `--task` / metadata, **or** an active task exists (resume) |
| Semantic verifier | off | `ARIADNE_ENABLE_SEMANTIC_VERIFIER` |
| Controlled delegation | off | `ARIADNE_ENABLE_CONTROLLED_DELEGATION` |
| Memory projection | off | `ARIADNE_ENABLE_MEMORY_PROJECTION` |

Hardening already delivered: fail-closed Web approval, read-only task attempts,
immutable goal binding, worker fencing/idempotency, skill attempt attribution,
context char accounting, bounded verifier reads, required oracles, locked
projection apply, hash-chained task revision events, **task_mode_resolved**
traces, protocol + attempt runtime extracted from turn (`tasks/runtime.py`:
tools payload, plan/replan control, capability attempt finalize), semi-e2e
FakeModel path, Web task banner, optional live e2e
(`ARIADNE_LIVE_CLOSED_LOOP=1`). Remaining gates: see design
Production-hardening backlog.

#### 14a — Verify + TaskState (P0)
- [x] `TaskState` persistence (local SQLite, task identity + active session
  pointer, optimistic revision) + resume re-check of
  workspace fingerprint / assumptions
- [x] Complete Observation / Assumption / CheckResult / PlanRevision contracts
- [x] Kernel `submit_task_plan` control call; strict schema and one material
  capability call per exchange
- [x] Full JSON Schema runtime validation; unknown/write actions never
  auto-retry
- [x] Step model: `preconditions`, `done_when`, `failure_policy`, retries
- [x] Deterministic checks: `command_exit`, `path_exists` / `path_absent`,
  `file_contains` (more kinds later)
- [x] Task mode on TurnApplication / CLI / Web; tool complete → verify step
- [x] Offline tests: fake model + file change + exit-code style verification

#### 14b — Replan + tool metadata (P0/P1)
- [x] Structured replan with append-only `plan_revisions` (must cite evidence)
- [x] Goal-level checks; `needs_input` when blocked on user
- [x] ToolSpec optional: `side_effect_level`, `network_access`, idempotency,
  `verification_hint`, richer
  failure codes (one registry only)
- [x] Approval consumes effect metadata; required credentials fail closed

#### 14c — Cognitive state + Context Compiler (P1)
- [x] Opt-in evidence-bound L2 projector (structured apply vs
  `confirmed_no_change`; no silent empty success)
- [x] Conflict / superseded / expired semantics for typed state fields, with
  authority ordering and as-of history restoration
- [x] ContextCompiler: deterministic budgeted assembly + attribution traces
  (source, reason, token chars, included|summarized|dropped); required evidence
  fails rather than truncates

#### 14d — Skill feedback + user model (P1)
- [x] Skill outcome ledger records candidate/load/explicit-adoption/tool/outcome/
  correction signals; selection adjustment has a minimum-sample gate, decay,
  explanation, bounded effect, and host disable switch
- [x] Evidence-backed skill patch proposal + generated diff + authenticated user
  confirm/reject + expected-version CAS + version snapshot (no model self-confirm)
- [x] Typed user model (preference / goal / capability / constraint / relation),
  scoped and revisioned, included in memory context with authenticated host edit API

#### 14e — Stretch (P2)
- [x] Optional structured semantic verifier, evidence-quoting and rejected when
  a deterministic oracle is present
- [x] Host-cron scheduled deterministic goal checks with SQLite leases,
  revisioned pause/resume/cancel, and user notifications
- [x] `image_file` environment verification of real bytes, format, dimensions,
  size, and SHA-256 evidence
- [x] Opt-in controlled 2–3-way advisory delegation through the one registry;
  delegates have zero capabilities and must quote parent evidence

### Phase 15 — Memory intelligence (personal vertical slice)

Design: [design/memory-intelligence.md](design/memory-intelligence.md).

**Status: functional personal-kernel vertical slice; major correctness
hardening landed (same-turn goal binding, quoted/spaced scalar secrets, full
v1 journal structural validation). Production/ranking hardening remains
usage-driven work — not multi-tenant “complete.”**

- [x] Deterministic-first automatic turn projector with ambiguity-only optional LLM
- [x] Typed preference supersession with temporal validity and evidence
- [x] Evidence-bound Episode events and decision/causal chains
- [x] Full-operation L2 replay for relation/status/collection as-of reads
- [x] Episode-aware search and constrained entity/relation/timeline traversal
- [x] Cross-session Reflection candidates; explicit accept/reject gate
- [x] Structured Prospective memory triggers; host owns external scheduling
- [x] Turn-level observability and grounded end-to-end tests
- [x] Action-bound Reflection confirmation contracts; negative text cannot accept
- [x] Host-owned Goal/Episode transitions, monotonic status authority, and no in-place terminal reactivation
- [x] Independent structured-secret redaction, including camelCase keys/scalar assignments and digest-only nested allowlist values
- [x] Recoverable capture journal, workspace/StateStore affinity fencing, Store idempotency, and bounded/fair next-turn recovery
- [x] Capture journal v2 migration; unrecoverable legacy pending rows are terminal `migration_required` quarantine records
- [x] Nonterminal failure/retry Episode lifecycle and strict LLM capture protocol
- [x] Windowed Episode hits, stable event ids, paged evidence, and total byte caps

## Explicitly deferred forever (unless product changes)

- Company Pack system (superseded by official optional plugins)
- WeCom/Feishu/Telegram connectors in core
- enterprise mail/egress gateway mesh as required runtime
