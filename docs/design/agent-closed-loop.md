# Design: Closed-Loop Agent Execution (Personal / 2C)

Status: **active design** (not fully implemented)  
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

---

## 1. Problem statement

| Strength today | Gap |
| --- | --- |
| Graded `memory_search`, scopes, as-of | L2 projection thin / opt-in; cognition ≠ search hits |
| Skill hybrid selection + digests | Little use→outcome feedback into ranking |
| Tool loop + approval + schema metrics | Tool success ≠ task / goal success |
| Layer budgets, deferred exposure | No unified Context Compiler with adoption traces |
| Session / atelier persistence | Resume is mostly transcript, not structured task state |

Without plan contracts and verification, complex tasks degrade into
“LLM improvises until loop limit.”

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

Stored under host data dir (JSON or SQLite), keyed by `session_id` (+ optional
task id). Not a second chat log.

```text
TaskState
  task_id: str
  session_id: str
  user_id: str
  status: active | blocked | completed | failed | cancelled
  goal: str                         # user-facing objective
  goal_checks: list[Check]          # goal-level verification
  steps: list[Step]
  current_step_id: str | None
  last_observation: Observation | None
  open_questions: list[str]         # needs_input
  workspace_fingerprint: str        # e.g. git HEAD / mtime hash
  assumptions: list[Assumption]     # must re-check on resume
  updated_at: float
```

### 4.1 Step

```text
Step
  step_id: str
  intent: str
  status: pending | running | verified | failed | skipped
  preconditions: list[Check]
  done_when: list[Check]            # required for verified
  tools_hint: list[str]             # optional, not a second registry
  max_retries: int
  failure_policy: retry | replan | ask_user | abort
  evidence: list[EvidenceRef]
  attempt: int
```

### 4.2 Check / Evidence

```text
Check
  kind: command_exit | path_exists | path_absent | file_contains
       | git_diff_matches | http_ok | json_path | llm_semantic | user_confirm
  spec: dict                        # kind-specific
  required: bool

EvidenceRef
  kind: tool_result | command | file_diff | test_log | memory_hit | user
  ref: str                          # turn_id, path, tool_call_id, ...
  summary: str                      # short, non-invented
  at: float
```

**Rule:** `status=verified` only when all required `done_when` checks pass.
Tool invoke success alone never promotes a step to verified.

### 4.3 Resume

On process restart / `/resume`:

1. Load `TaskState` (if any) + recent transcript.
2. Recompute `workspace_fingerprint`; if changed, mark assumptions **stale**.
3. Re-run checks for `current_step` and open goal checks before more tools.
4. Do **not** treat chat history as proof that external world is unchanged.

---

## 5. Plan → act → verify → replan loop

### 5.1 Modes

| Mode | When | Behavior |
| --- | --- | --- |
| **direct** (default) | Short / low-risk turns | Existing tool loop; optional light checks |
| **task** | Multi-step, mutating, or host flag | Explicit TaskState + step verification |

Activation (host policy), examples:

- User or CLI: `--task` / “plan and verify”
- Heuristic: many tools, approval-mode on-request, test/edit verbs
- Always-on later only if latency budget allows

### 5.2 Control flow (task mode)

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

### 5.3 Replan

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

---

## 7. Memory integration (cognitive state)

Search remains on-demand ([memory-search.md](memory-search.md)). Closed-loop
adds **write discipline**:

| Write | Rule |
| --- | --- |
| L2 projection | **Opt-in** (`enable_memory_projection`); ops need evidence quotes; no silent empty `no_change` success |
| Conflicts | New version + `superseded_by` / status; no blind overwrite of authoritative fields |
| Types | Prefer typed slots: fact \| preference \| goal \| hypothesis (extensible) |
| Curated L3 | Explicit tool or user-confirmed consolidation apply |
| Episodic | Continue dual-write user/workspace indexes; search is not state |

User-visible “why remembered / why updated” is a host UI concern; kernel stores
`source_turn_id`, evidence, and timestamps.

---

## 8. Skills: outcome feedback (not auto-rewrite)

Record per turn (extends `SkillEvent` / side log):

```text
skill_name, candidate_score, loaded, adopted,
tool_names_used, step_verified | task_failed, user_corrected
```

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
- `ContextAttribution[]`: source, reason, score, token_chars, included|summarized|dropped  

### 9.3 Principles

- Deferred detail remains default ([DESIGN_PRINCIPLES §5](../DESIGN_PRINCIPLES.md))  
- Evidence required for verify stays **verbatim** (no aggressive summarize)  
- Schema cost metrics already exist — Compiler must emit comparable traces  

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

---

## 12. Phased delivery

### Phase 14a — Verify + TaskState (P0)

- [ ] `TaskState` store + resume re-check  
- [ ] Step `done_when` with deterministic checks (command_exit, path_*, file_contains)  
- [ ] Turn integration: task mode flag; on tool complete → verify current step  
- [ ] failure_policy: retry / ask_user / abort (replan stub ok)  
- [ ] Tests: fake model + file edit + pytest-style exit check  

### Phase 14b — Replan + tool metadata (P0/P1)

- [ ] Structured replan with `plan_revisions`  
- [ ] ToolSpec `side_effect_level`, `verification_hint`, richer errors  
- [ ] Goal-level checks + `needs_input` when blocked  

### Phase 14c — Projector + Context Compiler (P1)

- [ ] Opt-in evidence-bound L2 projector (no silent no_change success)  
- [ ] Conflict / superseded semantics  
- [ ] ContextCompiler module + attribution traces  

### Phase 14d — Skill feedback + user model (P1)

- [ ] Skill outcome ledger → ranking  
- [ ] Skill patch proposal + user confirm + version  
- [ ] Typed user model fields + host edit API  

### Phase 14e — Stretch (P2)

- [ ] Optional verifier model for semantic checks only  
- [ ] Proactive / scheduled goal push (host cron)  
- [ ] Multimodal environment checks  
- [ ] Controlled multi-agent (only after single-agent loop is solid)  

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
| Persistence | Local JSON/SQLite TaskState | Enough for personal resume |
| Multi-agent | Deferred | Complexity without closed loop |

---

## 15. Related

- [turn-lifecycle.md](turn-lifecycle.md) — current turn sequence  
- [memory-v1.md](memory-v1.md) — L0–L5 authority  
- [memory-search.md](memory-search.md) — graded retrieval  
- [memory-scopes.md](memory-scopes.md) — user / workspace / session  
- [../ROADMAP.md](../ROADMAP.md) — Phase 14 checklist  
