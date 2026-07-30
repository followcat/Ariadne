# Toolcall

## 1. Purpose

Toolcall is how Ariadne turns model decisions into **audited side effects**.

Goals:

- correct tool choice and arguments
- safe loop semantics
- low schema token tax
- one registry for all callables

## 2. Capability model

### 2.1 CapabilitySpec

```python
@dataclass
class CapabilitySpec:
    name: str
    title: str
    description: str                 # short catalog phrase
    tool_schema: dict                # full callable schema for the model API
    kind: str = "tool"               # tool | system_action | ...
    exposed_to_llm: bool = True
    tool_exposure: Literal["eager", "named_deferred", "hidden"] = "eager"
    required_credentials: tuple[str, ...] = ()
    side_effect_level: Literal["none", "read", "write", "destructive", "unknown"]
    network_access: Literal["none", "outbound", "unknown"]
    idempotent: bool | None
    failure_codes: tuple[str, ...] = ()
    verification_hint: tuple[str, ...] = ()
```

Layering rule (critical):

| Layer | Content |
| --- | --- |
| `description` (catalog) | short discovery phrase only |
| `tool_schema.description` | when/how to call, side effects, recovery |
| parameter descriptions | field meaning only |
| core policy | cross-tool rules (batching, priority) |

Do **not** copy long policies into every catalog line.

### 2.2 CapabilityRegistry

Responsibilities:

- hold all specs
- filter visibility for a session
- build `ToolExposureState` for a turn
- resolve handlers by name
- validate arguments with Draft 2020-12 JSON Schema at runtime
- fail closed when `required_credentials` are absent

There is no second registry for “native”, “plugin”, or “benchmark” tools.

### 2.3 ToolExposureState

Tracks wire state for one turn/loop:

```text
request_tools:           schemas currently offered to the model
deferred_tools:          full schemas available via search/load
callable_function_names: names legal to invoke now
loaded_tool_names:       deferred tools already materialized
client_search_mode:      none | function | native
```

Operations:

- `load_exact(tool_names)` materializes deferred schemas
- unknown names fail fast on invoke

## 3. Exposure strategies

### 3.1 Eager (small setups)

All tool schemas sent every exchange. Fine for ~few tools; collapses as tool count grows.

### 3.2 Deferred (design center)

```text
Initial request:
  - short capability catalog in prompt
  - only eager tools + tool_search (or equivalent)
  - deferred tools absent as full schemas

When model requests details / calls search:
  - materialize exact tool schemas into subsequent exchanges
```

Evidence from production toolcall labs:

- full function schemas can exceed 10k normalized JSON characters even for ~11 tools
- continuations often resend the same bulk schemas
- correctness cases and schema-cost cases must be scored separately (do not “win” cost by deleting tools)

### 3.3 Hidden

Not model-visible; may still be host-invoked (rare in personal v1).

## 4. Tool loop

```text
model response
  -> extract tool calls
  -> for each call:
       authorize/validate name & args
       dispatch handler
       freeze output / error
  -> append tool results to next model input
  -> repeat until final message or loop limit
```

Rules:

1. Loop limit is mandatory (`ARIADNE_TOOL_LOOP_LIMIT`).
2. Unknown tool → structured error (optionally return error tool result vs fail turn; choose one policy and document it; default **error tool result once, then allow model recovery**, but never invent a handler).
3. Parallel tool calls: allow only when handlers are side-effect safe; otherwise sequential.
4. Traces store args/outputs with redaction hooks for secrets.
5. Approval consumes ToolSpec effect metadata, not a hard-coded tool-name list.
6. Missing effect metadata means `unknown`, never safe. Dynamic tools resolve
   effects from validated arguments (for example GET=read, DELETE=destructive).
7. Task mode accepts at most one material capability call per model exchange
   and verifies it before another material call.

## 5. Builtin tools (personal kernel candidates)

| Tool | Role |
| --- | --- |
| `memory` | curated durable memory ops |
| `search_skills` | skill discovery |
| `load_skill` | skill body load |
| `sandbox.exec` | run command in sandbox port |
| `tool_search` | materialize deferred tool schemas |
| optional progress | host UX progress events |

Enterprise tools (business systems, mail grants, platform send) are **not** Ariadne builtins.

## 6. Handler contract

```python
class ToolHandler(Protocol):
    async def __call__(
        self,
        args: dict[str, object],
        ctx: InvocationContext,
    ) -> ToolHandlerResult: ...
```

`InvocationContext` includes session ids, sandbox handle, memory façade, trace sink — not HTTP request objects.

## 7. Testing strategy (from toolcall branch lessons)

Maintain cases with frozen contracts:

1. **Functional correctness** — right tool, args, order, no illegal side effects
2. **Schema efficiency** — short catalog coverage, deferred detail, no useless continuation schema spam
3. **Regression controls** — things that must keep passing while optimizing cost

Admission style:

- stable_target / regression_control / flaky_watch / candidate_discovery
- never claim improvement without same case contract + comparable run metadata

Ariadne can start lighter, but should not lose the *discipline*.

## 8. Non-goals

- Per-business-system first-class tools in core
- Compensating bad selection by writing novels in tool descriptions
- Automatic tool invention from free text without registration

## 9. Implementation phases

1. Eager registry + loop + traces
2. Catalog short descriptions + policy separation
3. Deferred exposure + `tool_search`
4. Schema cost metrics in traces
5. Case suite for selection/cost
