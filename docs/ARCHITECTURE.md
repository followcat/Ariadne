# Architecture

## 1. Intent

Ariadne is an **application kernel** for running agent turns. Hosts (CLI, notebook, HTTP adapter, desktop app) call the kernel; the kernel does not require a specific host.

```text
Host (CLI / HTTP / app)
        |
        | RunTurnCommand
        v
+----------------------------------------------+
| Ariadne Kernel                               |
|                                              |
|  TurnApplication                             |
|    |- MemoryFacade.build_context             |
|    |- SkillSelector.plan                     |
|    |- ToolExposure.plan                      |
|    |- ModelPort.complete / stream            |
|    |- ToolLoop (invoke via ToolRuntime)      |
|    |- MemoryFacade.commit / schedule_writes  |
|    '- TraceSink.record                       |
|                                              |
|  Ports: Model, MemoryStore, SkillStore,      |
|         ToolHandlers, Sandbox, Clock         |
+----------------------------------------------+
        |
        | adapters
        v
 Local FS / SQLite / Docker / OpenAI-compatible APIs
```

## 2. Logical packages (target)

```text
ariadne/
  kernel/                 # pure application use cases and domain types
    turn.py               # TurnApplication
    types.py              # commands, events, errors
    policy.py             # core agent policy text assembly hooks
  skills/
    store.py              # load/validate skill packs
    outcomes.py           # decayed, explainable use→outcome ledger
    patches.py            # propose / confirm / versioned write
  tools/
    models.py             # CapabilitySpec
    registry.py           # CapabilityRegistry + ToolExposureState
    runtime.py            # dispatch authorized invocations
    builtin/              # memory, progress, sandbox.exec, ...
  memory/
    facade.py             # layered MemoryContext assembly
    state.py              # evidence-bound conversation state
    user_model.py         # typed editable personalization
  context/
    compiler.py           # prompt budget + attribution
  sandbox/
    port.py               # SandboxPort protocol
    null.py               # no-op / disabled
    # local.py, docker.py later — redesignable
  adapters/
    model_openai.py
    store_sqlite.py
    store_files.py
  host/
    cli.py                # optional
```

Import rule:

- `kernel` must not import FastAPI, Docker SDK details, or host frameworks.
- adapters implement ports; bootstrap/composition root wires them.
- skills/tools/memory may depend on kernel types and ports, not on HTTP.

## 3. Turn lifecycle

```text
1. Accept RunTurnCommand
2. Resolve session (user_id / session_id) — personal v1 may be trivial
3. Build MemoryContext for query
4. Plan skill selection (auto / recommended / other / none)
5. Plan tool exposure (eager + deferred + search tool if needed)
6. Assemble model request:
     policy + memory layers + skill plan + user input + tools
7. Model exchange loop:
     a. stream/complete
     b. if tool calls:
          - validate names against callable set
          - invoke handlers (may use sandbox)
          - append tool results
          - continue until final or loop limit
     c. if final assistant message: stop
8. Persist traces, schedule memory writes
9. Return TurnResult + events
```

**Closed-loop (Phase 14):** optional **task mode** inserts persisted plan / step
verification and evidence-bound replan around the tool loop. An opt-in strict L2
projector and the shared `ContextCompiler` add evidence-bound state plus prompt
attribution. Outcome-aware skills, typed personalization, optional semantic/
image verification, host-scheduled checks, and bounded advisory delegation
form the functional 14a–e vertical slice. Production-hardening gates remain — see
[design/agent-closed-loop.md](design/agent-closed-loop.md). Default short turns
remain the direct loop above.

Only **TurnApplication** is the public use-case entry for execution. Internal helpers (model client, registries) are not second entries for hosts.

## 4. Core domain objects

### 4.1 RunTurnCommand

```text
session_id: str
user_id: str | None          # optional for personal single-user
input: list[MessagePart]     # text/files later
model: str | None            # override
tool_loop_limit: int
stream: bool
metadata: dict               # host baggage, not interpreted as company routing
```

### 4.2 TurnResult

```text
turn_id / response_id
status: completed | failed | needs_input
text: str
tool_calls: list[ToolCallTrace]
skill_events: list[SkillEvent]
memory_contribution: MemoryContextSummary
usage: Usage
error: AppError | None
```

### 4.3 Engine events (internal to host)

Suggested event families:

| Event | Meaning |
| --- | --- |
| `ModelOutputDelta` | text/reasoning deltas |
| `ToolCallStarted` | tool registered for execution |
| `ToolCallCompleted` | tool result frozen |
| `SkillLoaded` | skill body/assets loaded |
| `MemoryLayerUsed` | which layers contributed |
| `TurnCompleted` / `TurnFailed` | terminal |

Hosts map these to SSE, CLI printers, or logs.

## 5. Skills subsystem

See [SKILLS.md](SKILLS.md).

Architectural constraints:

- Skill catalog is data + markdown, not executable plugin code in-process (except validated builtin tool handlers registered separately).
- `search_skills` / `load_skill` are tools in the **same** registry.
- Skill selection can inject short plans into the prompt; full bodies prefer tool_result scope (turn-scoped) over permanent system-prompt growth.

## 6. Toolcall subsystem

See [TOOLCALL.md](TOOLCALL.md).

Architectural constraints:

- `CapabilitySpec` is the unit of registration.
- `ToolExposureState` tracks what is on the wire vs deferred vs loaded.
- Continuations must not blindly resend irrelevant full schemas forever (design target: deferred + search).
- Tool loop limit is enforced in TurnApplication.

## 7. Memory subsystem

See [MEMORY.md](MEMORY.md).

Architectural constraints:

- `MemoryFacade.build_context` is the only read entry used by TurnApplication.
- Layer failures should be explicit in context metadata; do not hide with fake empty success when the layer was required.
- Writes (curated add, semantic index, state projection) are explicit operations with clear durability semantics.

## 8. Sandbox subsystem

See [SANDBOX.md](SANDBOX.md).

Architectural constraints:

- Kernel depends only on `SandboxPort`.
- Default distribution may ship a simple local backend or even disable sandbox until redesigned.
- Enterprise egress/mail gateways are **not** part of Ariadne core architecture.

## 9. Model port

```text
class ModelPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

`ModelRequest` is Ariadne-owned. Provider-specific translations (OpenAI Responses, Chat Completions, etc.) live in adapters.

## 10. Composition root

Personal bootstrap example:

```text
config
  -> create ModelPort
  -> create Memory stores
  -> load SkillStore from ./skills
  -> build ToolRegistry (builtins + user tools)
  -> optional SandboxPort
  -> TurnApplication
  -> CLI or thin HTTP host
```

No automatic plugin discovery of untrusted code. Explicit wiring only.

## 11. What we deliberately remove vs enterprise cores

| Enterprise concern | Ariadne stance |
| --- | --- |
| Company Pack manifests | Absent |
| Connector identity mapping | Host problem |
| Business capability via egress adapters | Absent from core |
| Multi-tenant service tokens | Optional host auth only |
| Confirmation/grant control plane | Optional later; not MVP core |
| Mail/SMB/browser microservices | Not kernel-owned |

## 12. Consistency with evidence-driven work

The AIFlow `skills` / `toolcall` / `memory` branches taught concrete lessons Ariadne should keep:

1. **Skill index tax** — too many skills in prompt destroys tool attention.
2. **Eager full schemas** — large tool sets burn tokens every continuation.
3. **Memory is multi-failure-mode** — capacity, multi-entity state, multi-hop recall, stale updates need explicit layers/tests.
4. **One registry** — never fork tools for benchmarks.

Ariadne will re-implement these as a clean personal kernel, not as a company distribution split.

## 13. Related docs

- [PUBLIC_API.md](PUBLIC_API.md)
- [SKILLS.md](SKILLS.md)
- [TOOLCALL.md](TOOLCALL.md)
- [MEMORY.md](MEMORY.md)
- [SANDBOX.md](SANDBOX.md)
- [ROADMAP.md](ROADMAP.md)
- [SOURCE_MAP.md](SOURCE_MAP.md)
