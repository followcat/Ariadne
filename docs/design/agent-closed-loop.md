# Design: Closed-Loop Agent Execution (Personal / 2C)

Status: **functional vertical slice complete; production hardening pending**
Audience: implementers  
Related: [../ARCHITECTURE.md](../ARCHITECTURE.md), [turn-lifecycle.md](turn-lifecycle.md),
[../MEMORY.md](../MEMORY.md), [memory-v1.md](memory-v1.md),
[../TOOLCALL.md](../TOOLCALL.md), [../SKILLS.md](../SKILLS.md),
[../DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md), [../NON_GOALS.md](../NON_GOALS.md)

## 0. Purpose

Ariadne today is strong at **finding** (memory search, skill selection) and
**calling tools**. The next quality leap is not more retrieval alone, but a
**closed loop**:

```text
understand goal
  → plan (verifiable steps)
  → act (one tool / one step)
  → verify (evidence, prefer deterministic)
  → update world / task state
  → continue | retry | replan | ask user
```

**North star:** after every material action, verify with evidence; update the
plan from that evidence — not from “the tool returned 200.”

This doc is **personal 2C / single-agent kernel**. It is not a multi-agent mesh,
not Temporal, and not an enterprise workflow product.

The Phase 14a–e checkmarks below mean that the 2C kernel trajectory exists and
is covered by offline fixtures. They are not a ToB production-readiness claim.
In particular, tenant IAM/isolation remains out of scope; lifecycle control,
trusted high-risk goal oracles, verifier authorization ports, provider-exact
token budgeting, and operational fault gates remain hardening work.

---

## 1. Problem statement

| Strength today | Remaining gap |
| --- | --- |
| Graded `memory_search`, scopes, as-of | Conflict UX / default projection still opt-in |
| Skill hybrid + outcome ledger + patch confirm | Ranking learning needs more real traffic |
| **Task mode** plan/verify/replan vertical slice | Default path still direct unless `--task` or active task |
| ContextCompiler + attributions | Provider-exact token budgets still approximate |
| SQLite TaskState + resume fingerprint | Default product prompts / multi-session UX |

Vertical slice is offline-tested. The open problem is **default-path product
quality** (when to enter task mode, observability, thin turn orchestration),
not missing modules.

---

## 2. Non-goals (this track)

- Multi-agent debate / specialist swarms as default
- Expanding vector stores as a substitute for conflict/time/evidence
- Unsupervised rewrite of skills, system policy, or curated memory
- Mandatory LLM projector every turn (contradicts honest L2 default)
- Distributed workflow engines (Temporal, etc.) for v1
- Second capability registry or parallel “planner service” outside TurnApplication

---

## 3. Architecture (conceptual)

```text
                    TurnApplication
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
    MemoryFacade     TaskController    ToolRegistry
    (L0–L4, search)  (plan/verify)     (invoke + meta)
          |                |                |
          +--------+-------+--------+-------+
                   |
                   v
            ContextCompiler
            (budgeted assembly + attribution)
```

| Component | Role |
| --- | --- |
| **TaskController** | Owns `TaskState`: goal, steps, observations, open questions |
| **Verifier** | Checks step/goal against evidence (deterministic first) |
| **ContextCompiler** | Chooses what enters the model request under token budgets |
| **MemoryFacade** | Read/write knowledge; optional evidence-bound L2 project |
| **ToolRegistry** | Acts; richer ToolSpec metadata for planning |

Only **TurnApplication** remains the public execution entry ([ARCHITECTURE](../ARCHITECTURE.md)).

---

## 4. TaskState (lightweight persistence)

Stored in host-local SQLite under the data dir. `task_id` is the durable
identity; a separate `session_id → active_task_id` pointer selects the task to
resume. A session may retain multiple terminal tasks, but has at most one
active / needs-input task. It is not a second chat log.

```text
TaskState
  schema_version: int               # v2; v1 requires explicit operator handling
  revision: int                     # optimistic concurrency token
  task_id: str
  session_id: str
  user_id: str | None
  status: active | needs_input | completed | failed | cancelled
  original_user_goal: str           # immutable authoritative request
  goal: str                         # must exactly preserve original_user_goal
  goal_checks: list[Check]          # non-empty; at least one required oracle
  steps: list[Step]
  current_step_id: str | None
  last_observation: Observation | None
  open_questions: list[OpenQuestion]
  workspace_fingerprint: str        # scoped tree fingerprint; not evidence
  assumptions: list[Assumption]     # must re-check on resume
  plan_revisions: list[PlanRevision] # append-only
  created_at: float
  updated_at: float
```

### 4.1 Step

```text
Step
  step_id: str
  intent: str
  status: pending | running | verified | failed | skipped
  preconditions: list[Check]
  done_when: list[Check]            # non-empty; at least one required oracle
  tools_hint: list[str]             # optional, not a second registry
  max_retries: int
  failure_policy: retry | replan | ask_user | abort
  evidence: list[EvidenceRef]
  attempt: int
  check_results: list[CheckResult]
```

TaskState v1 is a known incompatible predecessor: it did not preserve an
authoritative original user goal or a mandatory goal oracle, so the kernel must
not invent those fields during migration. Loading v1 fails with
`ARIADNE_TASK_SCHEMA_MIGRATION_REQUIRED`; the operator must explicitly abandon
or export the legacy task. Unknown versions fail with
`ARIADNE_TASK_SCHEMA_UNSUPPORTED`.

Every saved revision also appends a hash-chained event. Task loads and updates
verify continuous revisions, every event digest, and exact equality between
the chain tail and the current SQLite snapshot. A mismatch fails closed with
`ARIADNE_TASK_AUDIT_MISMATCH`; replay history is not merely advisory.

### 4.2 Check / Evidence

```text
Check
  check_id: str
  kind: command_exit | path_exists | path_absent | file_contains
       | llm_semantic | image_file
  spec: dict                        # kind-specific
  required: bool

EvidenceRef
  evidence_id: str
  kind: tool_result | command | file_diff | test_log | memory_hit | user
  ref: str                          # turn_id, path, tool_call_id, ...
  summary: str                      # short, non-invented
  attempt_id: str
  at: float

CheckResult
  check_id: str
  status: pass | fail | error | stale | not_run
  evidence: list[EvidenceRef]
  observed_value: any | None
  error: AppError | None
  checked_at: float

Observation
  observation_id: str
  kind: tool_result | check_result | user | environment
  summary: str
  evidence: list[EvidenceRef]
  at: float

Assumption
  assumption_id: str
  text: str
  status: current | stale | invalid
  recheck: Check | None
  checked_at: float | None

OpenQuestion
  question_id: str
  prompt: str
  asked_at: float

PlanRevision
  revision: int
  reason: str
  evidence: list[EvidenceRef]        # required; no evidence-free replan
  prior_step_ids: list[str]
  new_step_ids: list[str]
  at: float
```

`git_diff_matches`, `http_response`, `json_path`, and `user_confirm` remain
reserved contract kinds; they are not exposed by the current plan schema.
Filesystem verification is workspace-confined and size-bounded, but routing
all active reads through a dedicated authorized `VerificationPort` is still a
hardening item.

**Rule:** `status=verified` only when all required `done_when` checks pass.
Tool invoke success alone never promotes a step to verified.

### 4.3 State transitions and concurrency

Allowed task transitions:

```text
active → needs_input | completed | failed | cancelled
needs_input → active | failed | cancelled
completed | failed | cancelled → terminal (no mutation except archival metadata)
```

Store writes use an expected `revision`; a stale writer fails with
`ARIADNE_TASK_CONFLICT`. The target lifecycle rule is that new user input never
silently replaces an active goal and the host explicitly continues, cancels,
or starts a new task. The current slice preserves the active goal but still
auto-continues `needs_input`; the explicit lifecycle surface remains in the
hardening backlog.

Each saved revision is also appended to `task_events` with a payload digest and
previous-event digest. This provides integrity-checked local replay while the
`tasks` row remains the current-state projection.

Step transitions are `pending → running → verified|failed`, with `skipped`
allowed only through an evidence-citing replan. Check ERROR never becomes
FAIL or PASS.

### 4.4 Verification boundary

Verifier checks consume existing evidence by default. A check must never
execute a command, read outside the configured workspace root, or issue a
network request behind `ToolRegistry` / sandbox / approval. In particular,
`command_exit` references an already-recorded tool call; it is not an
arbitrary command runner.

Any active verification read is a normal registered tool call and passes the
same authorization path as other actions. `http_response` is tool-level
evidence only and cannot, by itself, complete a mutating step or the goal.

### 4.5 Resume

On process restart / `/resume`:

1. Load `TaskState` (if any) + recent transcript.
2. Recompute `workspace_fingerprint`; if changed, mark assumptions **stale**.
3. Re-run checks for `current_step` and open goal checks before more tools.
4. Do **not** treat chat history as proof that external world is unchanged.

The fingerprint is a change detector, not completion evidence. File checks
are re-run; prior command/API results become stale unless a fresh registered
tool call reproduces them.

---

## 5. Plan → act → verify → replan loop

### 5.1 Modes

| Mode | When | Behavior |
| --- | --- | --- |
| **direct** | Default under `task_mode_policy=auto` with no active task | Existing tool loop |
| **task** | Host force, policy `on`, or active-task resume | Explicit TaskState + step verification |

**Activation policy** (`ARIADNE_TASK_MODE_POLICY` / Settings `task_mode_policy`):

| Policy | Behavior |
| --- | --- |
| `off` | Never unless metadata `task_mode=true` |
| `on` | Always task mode |
| `auto` (default) | Metadata wins; else enable if an **active task** already exists for the session (resume without re-passing `--task`); otherwise direct |

CLI: `--task` forces metadata on for this run; `--task-mode-policy off|on|auto`
sets the policy. Kernel emits `task_mode_resolved` with
`{enabled, reason, policy}` every turn. Stretch features stay off by default:
semantic verifier, controlled delegation, memory projection.

### 5.2 Plan control protocol

Plans and replans use kernel control calls (`submit_task_plan`, later
`revise_task_plan`) with strict JSON schemas. They are not capabilities, do
not enter the public tool catalog, and cannot perform side effects. The kernel
assigns task / step / check IDs and rejects unknown check kinds or empty
required `done_when` lists.

In task mode each model exchange may contain at most one material capability
call. The kernel verifies it before another capability call is accepted. A
plain assistant answer cannot complete an unverified task; it transitions to
`needs_input` (with the text recorded as an open question) or fails with a
structured protocol error.

### 5.3 Control flow (task mode)

```text
if no TaskState or goal changed:
  propose plan (model or template) → structured Steps
  validate plan (every step has done_when)
  persist TaskState

loop until goal verified | failed | needs_input | loop limit:
  select current pending step
  check preconditions → else replan or ask_user
  allow model tool calls constrained to step intent
  on tool results:
    run Verifier on done_when
    if pass → step.verified; advance
    if fail:
      according to failure_policy:
        retry (attempt++) | replan | ask_user | abort
  update last_observation + optional memory project
```

### 5.4 Replan

Replan **must** cite evidence (failed check, new observation).  
Forbidden: silent full plan wipe without notes in TaskState history
(`plan_revisions[]` append-only).

---

## 6. Verification layers

| Level | Question | Prefer |
| --- | --- | --- |
| **Tool** | Args valid? Invoke ok? Structured error? | Schema + registry |
| **Step** | Does this step’s `done_when` hold? | Deterministic checks |
| **Goal** | Is the user request satisfied? | Goal checks + optional user_confirm |

Deterministic first: pytest exit code, file diff, path exists, JSON path,
HTTP status. **LLM semantic check** only when no cheap oracle exists; must
still return structured pass/fail + quote from evidence, never free invention.

Infra ERROR ≠ step FAIL (same discipline as memory eval).

Automatic retry is allowed only for `none` / `read` effects or tools with an
explicit idempotency contract. Unknown, write, and destructive actions default
to `max_retries=0`; a new approval is required when an operation changes.

---

## 7. Memory integration (cognitive state)

Search remains on-demand ([memory-search.md](memory-search.md)). Closed-loop
adds **write discipline**:

| Write | Rule |
| --- | --- |
| L2 projection | **Opt-in** (`enable_memory_projection`); ops need evidence quotes; distinguish confirmed_no_change from disabled/skipped/failed |
| Conflicts | New version + `superseded_by` / status; no blind overwrite of authoritative fields |
| Types | Prefer typed slots: fact \| preference \| goal \| hypothesis (extensible) |
| Curated L3 | Explicit tool or user-confirmed consolidation apply |
| Episodic | Continue dual-write user/workspace indexes; search is not state |

User-visible “why remembered / why updated” is a host UI concern; kernel stores
`source_turn_id`, evidence, and timestamps.

While a task is active, TaskState is authoritative for its goal and progress;
L2 is a derived projection and must not overwrite it.

For the personal local JSON backend, one coordinator file lock serializes the
final lease-token check, ConversationState CAS mutation, and projection job
completion. A worker that lost its lease therefore cannot mutate L2 before its
completion is rejected. This is a local transaction boundary, not a claim that
the two JSON documents are a general distributed transaction store.

---

## 8. Skills: outcome feedback (not auto-rewrite)

Record per turn (extends `SkillEvent` / side log), with adopted skills bound to
the concrete capability attempt they influence:

```text
skill_name, candidate_score, loaded, adopted,
task_id, step_id, attempt_id, tool_names_used,
step_verified | step_failed, task_outcome, user_corrected
```

One turn may adopt multiple skills for different steps. Each adopted skill
receives only its bound attempt outcome; an adoption with no bound capability
attempt cannot inherit the turn's latest step/task success.

**Allowed learning:** adjust selection ranking / demote noisy skills.  
**Disallowed by default:** agent silently edits skill body.

Safe loop:

1. Agent proposes skill patch + evidence + diff  
2. User confirms  
3. Versioned write under user skills root  
4. Optional small offline eval before default auto_load  

Aligns with [NON_GOALS](../NON_GOALS.md) soft item: no unsupervised skill-learning workers.

---

## 9. Context Compiler

Unifies assembly currently split across turn policy, memory budgets, skill plan,
and tool exposure.

### 9.1 Inputs

- TaskState (current step + goal)  
- MemoryContext layers  
- Skill plan / bodies  
- Tool catalog + schemas  
- Recent raw + open tool results  
- Token / char budgets  

### 9.2 Outputs

- Ordered prompt blocks for the model  
- `ContextAttribution[]`: source, reason, score, `token_chars`,
  included|summarized|dropped. The historical `token_chars` field currently
  reports complete serialized-message characters, not provider tokens.

### 9.3 Principles

- Deferred detail remains default ([DESIGN_PRINCIPLES §5](../DESIGN_PRINCIPLES.md))  
- Evidence required for verify stays **verbatim** (no aggressive summarize)  
- Schema cost metrics already exist — Compiler must emit comparable traces  
- Authority order is kernel policy > host policy > TaskState > user-confirmed
  curated memory > skills > retrieved memory > external/tool content
- Required evidence that does not fit the budget fails explicitly; it is never
  silently truncated or dropped
- Complete message metadata/tool-call arguments and the active tool schemas are
  included in the serialized request gate before every provider call

---

## 10. ToolSpec extensions (progressive)

Keep one registry. Add **optional** metadata (absent = unknown, not “safe”):

| Field | Use |
| --- | --- |
| `side_effect_level` | none \| read \| write \| destructive |
| `idempotent` | bool |
| `failure_codes` | documented structured codes |
| `verification_hint` | suggested Check kinds after invoke |
| `preconditions` | soft hints for planner (not a second auth system) |
| `effects` | soft postconditions |

Phase 14a also replaces the current required-fields-only validator with full
JSON Schema runtime validation. `required_credentials` becomes enforced host
context, not documentation. Approval remains a host decision but consumes
effect metadata; missing metadata is `unknown`, never safe.

Authorization stays separate from exposure ([DESIGN_PRINCIPLES §6](../DESIGN_PRINCIPLES.md)).

---

## 11. User model (2C)

Long-lived personalization is **typed memory**, not a bag of notes:

| Layer | Examples | Store |
| --- | --- | --- |
| Preferences | style, stack, habits | L3 user curated / typed entries |
| Active goals | current project aims | TaskState + optional L2 |
| Capability model | what user knows | L3 with low confidence until confirmed |
| Constraints | privacy, budget, forbidden ops | host policy + memory |
| Relations | people, repos, tasks | L2 relations / L3 |

Editable in host UI; every entry: type, source, confidence, scope, updated_at.

### 11.1 Optional stretch boundaries

The P2 features remain opt-in and constrained by the same single-agent kernel:

- `llm_semantic` is accepted only when the host configures a
  `SemanticVerifier`, the check supplies `oracle_unavailable_reason`, and that
  check group contains no deterministic oracle. The verifier exposes no
  capabilities and must return pass/fail plus a verbatim quote from existing
  tool evidence.
- `image_file` is a deterministic environment check: it reads a workspace-bound
  file, validates actual PNG/JPEG/GIF/WebP bytes, optional dimensions/format/
  size constraints, and records path plus SHA-256 evidence.
- Scheduled goals are explicit host-cron records, not autonomous conversations.
  SQLite leases fence runners; only read-only deterministic checks are allowed;
  each run records a pending/satisfied/error notification. Users can pause,
  resume, or cancel with revision CAS.
- Controlled delegation is disabled by default. When enabled,
  `delegate_analysis` fans out 2–3 distinct advisory subgoals. Delegates receive
  shared evidence and zero capabilities, return a structured evidence quote,
  and cannot mutate TaskState or complete the parent goal. The parent remains
  the sole actor and verifier.

Host switches: `ARIADNE_ENABLE_SEMANTIC_VERIFIER=1` and
`ARIADNE_ENABLE_CONTROLLED_DELEGATION=1`. Scheduled goals are managed through
authenticated `/api/me/scheduled-goals*` host endpoints.

---

## 12. Phased delivery

### Phase 14a — Verify + TaskState (P0)

- [x] `TaskState` SQLite store + resume re-check
- [x] Complete state/check/evidence contracts, revisions, and task events
- [x] Kernel `submit_task_plan` control-call protocol; one material call/exchange
- [x] Full JSON Schema argument validation and safe retry baseline
- [x] Step `done_when` with deterministic checks (command_exit, path_*, file_contains)
- [x] Turn integration: task mode flag; on tool complete → verify current step
- [x] failure_policy: retry / ask_user / abort (replan pauses for 14b)
- [x] Tests: fake model + file edit + command exit check

### Phase 14b — Replan + tool metadata (P0/P1)

- [x] Structured evidence-citing replan with append-only `plan_revisions`
- [x] ToolSpec side effect/network/idempotency/verification/failure metadata
- [x] Metadata-driven approval and enforced `required_credentials`
- [x] Goal-level checks + `needs_input` when blocked

### Phase 14c — Projector + Context Compiler (P1)

- [x] Opt-in evidence-bound L2 projector (no silent no_change success)
- [x] Conflict / superseded / expired semantics
- [x] ContextCompiler module + attribution traces

### Phase 14d — Skill feedback + user model (P1)

- [x] Skill outcome ledger → ranking
- [x] Skill patch proposal + user confirm + version
- [x] Typed user model fields + host edit API

### Phase 14e — Stretch (P2)

- [x] Optional verifier model for semantic checks only
- [x] Proactive / scheduled goal push (host cron)
- [x] Multimodal environment checks
- [x] Controlled multi-agent (only after single-agent loop is solid)

### Production-hardening backlog

- [x] Composition-root approval policy for every host; non-interactive
  `on-request` fails closed instead of executing material tools
- [x] Read-only task exchanges create attempts and run step/goal verification
- [x] Immutable `original_user_goal`, exact goal preservation, and at least one
  required check for every step and goal oracle
- [x] Projection/scheduler lease tokens, stale-owner CAS, unique Web worker IDs,
  projection job idempotency keys, and coordinator-locked projection apply
- [x] Skill outcomes bind each adopted Skill to its own capability attempt and
  step instead of scanning task history or sharing the turn's latest attempt
- [x] Complete serialized-message/tool-schema char gate, bounded verifier reads,
  fail-fast Web trace persistence, and load-gating hash-chained TaskState
  revision events with an explicit incompatible-v1 policy
- [ ] Trusted host/user-approved oracle for high-risk goal completion; planner
  still proposes the mandatory goal checks
- [ ] Explicit inspect / continue / cancel / abandon / archive host APIs with
  authorization and revision CAS; `needs_input` continuation is not yet a full
  lifecycle surface
- [ ] Dedicated authorized `VerificationPort` plus implemented diff / JSON path /
  HTTP / user-confirm checks
- [ ] Provider tokenizer/model-window accounting (the current gate is complete
  serialized character accounting, not exact provider tokens)
- [ ] Retention/query/export policy and fault-injection/crash-recovery CI gates;
  shared multi-tenant IAM/isolation remains a product non-goal

---

## 13. Success metrics

| Metric | Intent |
| --- | --- |
| Step verify precision | Failed checks catch false “done” |
| Task complete rate on multi-step fixtures | Offline fake-model scenarios |
| Replan with evidence rate | No blind replans |
| Context token / useful adoption | Compiler quality |
| Skill false-load rate | Selection learning |
| User override rate on memory/skill patches | Trust |

Phase gates also track false-complete rate, duplicate side effects, resume
correctness, direct-mode latency regression, task-mode token/tool-call cost,
unnecessary task activation, and replan-after-evidence success. Fake-model
tests prove state-machine behavior; fixed trajectory fixtures measure agent
outcomes. A phase is not complete while a false-complete or duplicate-effect
regression remains unexplained.

Infra ERROR ≠ product FAIL.

---

## 14. Decision record

| Decision | Choice | Why |
| --- | --- | --- |
| Agent shape | Single agent + TaskController | Cost, attribution, 2C kernel |
| Default path | direct tool loop | Latency for simple turns |
| Verification | Deterministic first | Accuracy + token cost |
| L2 project | Opt-in, evidence-bound | Honest memory defaults |
| Skill learning | Propose + confirm | Avoid error reinforcement |
| Persistence | Local SQLite TaskState | Transactional revisions without a hosted service |
| Multi-agent | Optional bounded advisory only | Parallel analysis without a second actor/tool loop |

---

## 15. Related

- [turn-lifecycle.md](turn-lifecycle.md) — current turn sequence  
- [memory-v1.md](memory-v1.md) — L0–L5 authority  
- [memory-search.md](memory-search.md) — graded retrieval  
- [memory-scopes.md](memory-scopes.md) — user / workspace / session  
- [../ROADMAP.md](../ROADMAP.md) — Phase 14 checklist  
