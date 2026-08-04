# Design: Memory Intelligence Vertical Slice

Status: **functional personal-kernel vertical slice; major correctness
hardening landed (Host-owned task→goal binding, compositional scalar secrets,
v1/v2 journal structural validation and quarantine); production/ranking
hardening pending**
Audience: implementers
Related: [../MEMORY.md](../MEMORY.md), [memory-v1.md](memory-v1.md),
[memory-search.md](memory-search.md), [turn-lifecycle.md](turn-lifecycle.md)

## 0. Outcome

Ariadne should feel as if it remembers a developing situation, not merely as if
it can search old chat text. The vertical slice adds one evidence-bound write
path and one evidence-bound read path:

```text
completed turn
  -> deterministic extraction
  -> optional LLM extraction for ambiguous evidence only
  -> recoverable capture journal
  -> typed current-memory update
  -> episode/event append
  -> reflection candidate update
  -> prospective-trigger evaluation

memory_search
  -> turn chunks + episodes
  -> constrained entity/relation/timeline traversal
  -> bounded event window + stable event ids
  -> explicit paged evidence expansion
  -> real turn citations
```

This extends the existing L0-L4 stack. It does not introduce a second agent
loop, a second capability registry, or an enterprise memory control plane.

## 1. Automatic capture

The single turn-completion path invokes `AutomaticMemoryProjector` after raw,
summary, and semantic writes. Extraction is tiered:

1. Deterministic rules handle explicit, low-ambiguity changes such as
   `以后 Python 项目都用 uv，不用 poetry 了`.
2. An optional model is called only for ambiguous correction, reference, or
   multi-entity language.
3. Model output is a proposal over the supplied evidence. Every event and
   state change must quote text present in the current turn/tool evidence.

The model protocol is strict: a legitimate empty result is `{"events":[]}`.
Invalid JSON, wrong shapes, unknown fields, and over-limit arrays raise
`ARIADNE_MEMORY_CAPTURE_PROTOCOL`; they are not converted into an empty result.
An unknown capture status is likewise reported as a failed memory layer.

Explicit user preferences may update the typed user model automatically.
Temporary discussion and inferred patterns do not. Capture failure is reported
as a failed memory layer and never changes an otherwise completed task result.

Automatic capture is recoverable across the independently locked stores. A
turn-scoped journal records `user_model`, `state`, `episode`, `reflection`, and
`prospective` stage completion. Every target Store accepts a capture-scoped
idempotency key. Only after all stages are durable does the journal write its
completion marker. This is recoverable consistency, not a claim that multiple
JSON files form one ACID transaction. Before each new capture, the projector
resumes a bounded batch for the active `workspace_key`, ordered by the oldest
recovery timestamp. Each record is also fenced to an opaque StateStore identity
so a shared user journal cannot replay L2 state into a different workspace
store. Affinity mismatches and other failures are persisted with attempt
metadata and rotate behind other pending records. They mark the Memory layer
failed without blocking capture of the current turn.

Capture journal schema v2 requires StateStore affinity. During the v1-to-v2
migration, completed records and pending records that already carry an identity
remain active. A legacy pending row without identity cannot be recovered safely:
it moves atomically to `quarantined_records` with terminal status
`migration_required` and error code
`ARIADNE_MEMORY_CAPTURE_MIGRATION_REQUIRED`. It is excluded from recovery and
therefore cannot create an infinite transient-failure loop. Bounded quarantine
ids remain visible in `auto_capture` observability.

Task lifecycle binding is Host-owned. Task plan submit atomically persists an
immutable `task_id -> goal_id` entry before any task attempt can run. When the
session already has a lifecycle-bearing current goal (for example from an
earlier user goal phrase), the Host reuses that id; otherwise it materializes
`goal:<plan_turn_id>`. Model-facing state operations cannot write `task_id` or
the binding map. A terminal outcome with a task id must resolve exactly one
binding, otherwise it fails with `ARIADNE_MEMORY_GOAL_BINDING` and is not
allowed to fall back to the current pointer. A task created and verified in
one turn opens its goal before applying the terminal status, so it remains one
completed Episode.

## 2. Episode and causal memory

`EpisodeStore` groups consecutive session turns into an event record. Events
use a small, closed vocabulary:

- `problem`, `goal`, `hypothesis`
- `attempt`, `observation`, `error`
- `decision`, `outcome`
- `preference_change`, `workflow_signal`, `entity_change`

Each event contains real `session_id`/`turn_id` evidence references and optional
entities, relations, reason, previous value, and tool-call id. An episode keeps
the goal, attempts, observations, decisions, outcomes, related turns, and
workspace/session identity. `(session_id, turn_id)` is idempotent.

Outcome metadata distinguishes nonterminal failure, verified completion,
abandonment, and cancellation. A failed tool call is an `error`, not a terminal
outcome. Free-text user/Assistant language and ordinary tool output remain
episodic evidence only. In the current slice, only closed Task-verifier evidence
with authority `verified_check` closes an Episode or sets the authoritative
current goal to `done`; exact action-bound user confirmation is a future path.
Each lifecycle-bearing Goal has an immutable `goal:<turn_id>` identity.
`session:current_goal` is a Host-owned pointer entity, never the Goal itself;
creating B after completed A changes only the pointer. The host-only
`set_current_goal` operation atomically migrates a legacy fixed-id Goal to a
deterministic `goal:legacy:<digest>` identity before installing the pointer.

Tool arguments and outputs cross an independent Memory redaction boundary.
Sensitive structured keys are normalized across camelCase and separators
before matching. Scalar text uses a generic assignment parser and routes every
captured key through that same normalization/matching function, covering
provider-prefixed token/secret names without a second finite list. Known
GitHub (`ghp_`, `gho_`, `ghs_`, `ghr_`, `github_pat_`), Slack (`xoxb-`,
`xoxp-`, `xoxa-`, `xoxr-`), Hugging Face (`hf_`), xAI (`xai-`), Google
(`AIza`), npm (`npm_`), and PyPI (`pypi-`) token families are also redacted
by shape.
Authorization `Bearer`, `Basic`, `Token`, and `ApiKey` schemes have a separate
closed redaction pass, including short credentials. Episodes retain only
bounded scalar fields from the status allowlist; nested or oversized values
become digest-only markers.
Records keep payload digests and tool-call evidence references, never the full
raw payload. Disabling trace redaction never authorizes secret persistence in
Memory.

The causal chain is represented by event order and explicit `reason` /
`because` / `rejected_alternative` fields; it is not an unconstrained knowledge
graph generated from model imagination.

## 3. Temporal truth

Typed user-model entries have one active logical key per
`(scope, type, key, workspace/session discriminator)`. Updates retain:

- `valid_from` and `valid_until`
- `previous_value` and `change_reason`
- structured evidence references
- revision history and status

Conversation-state versions persist complete closed operations. Point-in-time
reads replay new-format versions, so aliases, entity status, relations, and
collections follow the same cutoff as attributes. Legacy attribute-only history
continues to use the existing conservative reconstruction path.

## 4. Constrained multi-hop retrieval

Deep planners may request only these traversal operations:

- `resolve_entity`
- `follow_relation`
- `retrieve_timeline`
- `locate_decision`
- `locate_outcome`
- `expand_evidence`

The host executes the operations against stored episodes. A planner cannot
provide facts, event ids, or turn ids. Episode hits expose a representative real
turn, related turn ids, stable event ids, a matched-event window (at most three
events on each side), and citations for that window. The full response has a
64,000-byte UTF-8 hard cap. `memory_expand_evidence` pages stored events by
`episode_id` and `after_event_id`, with at most 16 events and 16,000 bytes per
page. Normal semantic turn search remains available and is merged with Episode
hits.

## 5. Reflection

Repeated grounded workflow/preference signals are aggregated across distinct
sessions. Reaching the configured threshold creates a `pending` reflection
candidate with evidence and observation/session counts.

```text
pending -> accepted | rejected
```

Pending candidates are rendered into memory context so the assistant can ask
the user. Only explicit `accept` promotes a model-inferred candidate into the
typed user model. Free-text substring consent is invalid. The model-facing tool
returns separate action-bound confirmation contracts containing candidate id,
action, a session-bound token, and the exact command the user must send. The
command must be the complete current user message; the decision records the
current turn. Negative language and a token for another action or session cannot
authorize acceptance.

## 6. Prospective memory

The kernel persists structured intentions with safe trigger fields:

- `workspace_equals`, `path_glob`, `text_contains`
- `tool_name`, `event_type`, `entity_id`

States are `pending | triggered | completed | cancelled`. The active host
supplies query/workspace/tool/path/event observations and the store performs
idempotent matching. Triggered reminders enter later memory context. Timers,
webhooks, and external polling remain host responsibilities.

## 7. Storage and observability

The personal default uses locked JSON stores under the memory root:

- `episodes.json`
- `capture_journal.json`
- `reflection.json`
- `prospective.json`
- existing `user_model.json`

Automatic capture emits an `auto_capture` `LayerReport` containing current
capture status, created ids, recovered capture ids, recovery counts, and a
bounded error note. A failed pending recovery marks this layer failed but does
not change the turn result. Reflection and prospective context each have their
own layer report. All search results remain grounded in real turn and session
ids.

Personal defaults are **automatic** without changing the kernel contract.
Hosts load one `MemoryLimits` object (profile → optional field overrides →
optional context scale) and pass it to every memory store.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ARIADNE_MEMORY_AUTO_CAPTURE` | on | run deterministic completed-turn capture |
| `ARIADNE_MEMORY_AUTO_CAPTURE_LLM` | on | allow an extra model call only on ambiguity signals |
| `ARIADNE_MEMORY_EPISODE_SEARCH` | on | merge Episode hits into `memory_search` |
| `ARIADNE_MEMORY_REFLECTION_SESSIONS` | 3 | distinct sessions required for a candidate |
| `ARIADNE_MEMORY_PROFILE` | `default` | `compact` \| `default` \| `deep` preset for all budgets |
| `ARIADNE_MEMORY_SCALE_TO_CONTEXT` | off | scale recent/layer budgets from context window |
| `ARIADNE_CONTEXT_MAX_CHARS` | 120000 | host prompt-context budget (scale reference) |
| `ARIADNE_MEMORY_RECENT_LIMIT` | (profile) | override recent raw messages per context build |
| `ARIADNE_MEMORY_LAYER_BUDGETS` | (profile) | partial JSON override of per-layer character budgets |
| `ARIADNE_MEMORY_EPISODE_MAX_EPISODES` | (profile) | maximum stored Episodes |
| `ARIADNE_MEMORY_EPISODE_MAX_EVENTS_PER_EPISODE` | (profile) | maximum events per Episode (alias `…_MAX_EVENTS`) |
| `ARIADNE_MEMORY_CAPTURE_MAX_RECORDS` | (profile) | maximum capture journal records |
| `ARIADNE_MEMORY_CAPTURE_RESUME_BATCH_SIZE` | (profile) | recovery batch (alias `…_RESUME_BATCH`) |

API helpers: `MemoryLimits.for_profile("default")`,
`limits.scaled_to_context(context_max_chars)`.

Values must be strict integers and are bounded by hard maxima (recent 128,
layer budget 120,000 characters, Episodes 8,192, events per Episode 256,
journal records 16,384, recovery batch 32). Invalid values fail with
`ARIADNE_CONFIG_INVALID`; they are never silently converted past the maximum.
A partial layer-budget object overrides known defaults while retaining other
layers; unknown layer names are rejected. Context scaling only adjusts
prompt-adjacent budgets (recent + layers); store capacities stay profile
safety ceilings.

The model-facing `conversation_state` read path uses an explicit safe view.
Host-only `task_goal_bindings` remain available to task completion and journal
recovery but are omitted from both returned JSON and rendered context.

## 8. Scope and remaining work

This slice is deliberately local and auditable. Major correctness hardening
covers consent binding, host-owned terminal authority, immutable task→goal
binding (no pointer fallback; same-turn completion and goal A/B episode
segments), structured secret redaction (including compositional assignments,
quoted multi-word values and spaced labels like ``API Key``), recoverable
capture journals with workspace affinity and v1/v2 structural
validation/quarantine, nonterminal
tool failures, strict extractor protocol, and evidence response budgets.

It does not claim production hardening for background scheduling, multi-device
sync, learned ranking, ontology induction, or multi-tenant governance.
