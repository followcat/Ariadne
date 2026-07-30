# Design: Memory Intelligence Vertical Slice

Status: **functional vertical slice; reviewed correctness hardening complete;
production hardening pending**
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
JSON files form one ACID transaction.

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
outcome. Only terminal evidence with authority `user_explicit`,
`tool_observed`, or `verified_check` closes an Episode. Assistant completion
language is retained as episodic `model_assertion` and cannot set the
authoritative current goal to `done`.

Tool arguments and outputs cross an independent Memory redaction boundary.
Sensitive structured keys are removed, and Episodes retain an allowlisted
status summary, payload digest, tool-call evidence reference, and no full raw
payload. Disabling trace redaction never authorizes secret persistence in
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

Automatic capture emits an `auto_capture` `LayerReport` containing status,
created ids, and a bounded error note. Reflection and prospective context each
have their own layer report. All search results remain grounded in real turn
and session ids.

Personal defaults are configurable without changing the kernel contract:

| Setting | Default | Purpose |
| --- | --- | --- |
| `ARIADNE_MEMORY_AUTO_CAPTURE` | on | run deterministic completed-turn capture |
| `ARIADNE_MEMORY_AUTO_CAPTURE_LLM` | on | allow an extra model call only on ambiguity signals |
| `ARIADNE_MEMORY_EPISODE_SEARCH` | on | merge Episode hits into `memory_search` |
| `ARIADNE_MEMORY_REFLECTION_SESSIONS` | 3 | distinct sessions required for a candidate |

## 8. Scope and remaining work

This slice is deliberately local and auditable. Reviewed correctness hardening
covers consent binding, authority, secret persistence, capture replay, Episode
lifecycle, strict extractor protocol, and evidence response budgets. It does
not claim production hardening for background scheduling, multi-device sync,
learned ranking, ontology induction, or multi-tenant governance. Ranking and
episode boundary quality should be improved from real usage traces after the
write/read path is exercised.
