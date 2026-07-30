# Public API

This document defines the **callable agent surface** Ariadne will expose. Names may tighten during implementation, but semantics should not silently change.

## 1. Design goals for the API

1. One primary call: run a turn.
2. Composition is explicit: model, skills, memory, tools, sandbox.
3. Streaming and non-streaming share the same command type.
4. Errors are structured and stable.
5. Hosts never need to reimplement the tool loop.

## 2. High-level façade

```python
from ariadne import Agent

agent = Agent(
    model=model_port,
    skills=skill_store,
    memory=memory_facade,
    tools=tool_registry,
    sandbox=sandbox_port,   # optional
    policy=policy_config,   # optional
)

result = await agent.run(
    "Hello",
    session_id="demo",
)
```

`Agent` is a thin façade over `TurnApplication`. Advanced hosts may use `TurnApplication` directly.

## 3. Core types

### 3.1 Messages

```python
@dataclass
class TextPart:
    text: str

@dataclass
class Message:
    role: Literal["user", "assistant", "system", "tool"]
    parts: list[TextPart]  # files/images later
```

Personal v1 may accept `str` and coerce to a single user `TextPart`.

### 3.2 RunTurnCommand

```python
@dataclass
class RunTurnCommand:
    session_id: str
    input: str | list[Message]
    user_id: str | None = None
    model: str | None = None
    stream: bool = False
    tool_loop_limit: int = 32
    metadata: dict[str, object] = field(default_factory=dict)
    # metadata["task_mode"]=True forces closed-loop task mode for this turn.
    # Host Settings.task_mode_policy: off | on | auto (default auto resumes
    # an active task without re-setting the flag). See design/agent-closed-loop.md.
```

### 3.3 TurnResult

```python
@dataclass
class TurnResult:
    turn_id: str
    status: Literal["completed", "failed", "needs_input"]
    text: str
    messages: list[Message]
    tool_calls: list[ToolCallTrace]
    skill_events: list[SkillEvent]
    memory: MemoryContextSummary
    usage: Usage
    error: AppError | None = None
    task: TaskSummary | None = None          # set when task mode ran
    context_attributions: list[...] = ...    # ContextCompiler decisions
    skill_pins: dict[str, str] = ...         # loaded skill digests
```

Task-related stream events include `task_mode_resolved`, `task_started`,
`task_resumed`, `task_step_started`, `task_check_completed`, `task_replanned`,
`task_needs_input`, `task_completed`, `task_failed`.

### 3.4 ToolCallTrace

```python
@dataclass
class ToolCallTrace:
    call_id: str
    name: str
    arguments: dict[str, object]
    output: object | None
    status: Literal["completed", "failed"]
    error: AppError | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

### 3.5 AppError

```python
@dataclass
class AppError:
    code: str          # e.g. ARIADNE_UNKNOWN_TOOL
    message: str
    details: dict[str, object] = field(default_factory=dict)
    retriable: bool = False
```

Fastfail examples:

| Code | When |
| --- | --- |
| `ARIADNE_UNKNOWN_TOOL` | model called unregistered / non-callable tool |
| `ARIADNE_INVALID_TOOL_ARGS` | schema validation failed |
| `ARIADNE_TOOL_LOOP_LIMIT` | exceeded configured loop limit |
| `ARIADNE_SKILL_NOT_FOUND` | load/search target missing |
| `ARIADNE_SKILL_INVALID` | pack validation failed |
| `ARIADNE_MEMORY_NOT_READY` | required projection/layer incomplete |
| `ARIADNE_SANDBOX_DISABLED` | sandbox tool used without backend |
| `ARIADNE_MODEL_ERROR` | provider failure mapped cleanly |

## 4. Agent methods

### 4.1 `run`

```python
async def run(
    self,
    input: str | list[Message],
    *,
    session_id: str,
    user_id: str | None = None,
    model: str | None = None,
    tool_loop_limit: int | None = None,
    metadata: dict[str, object] | None = None,
) -> TurnResult:
    ...
```

### 4.2 `run_stream`

```python
async def run_stream(...) -> AsyncIterator[TurnEvent]:
    ...
```

Events include model deltas, tool lifecycle, and a final `TurnCompleted` / `TurnFailed` carrying `TurnResult`.

### 4.3 Inspection helpers (optional v1)

```python
await agent.list_skills()
await agent.list_tools(session_id=...)
await agent.memory.get_curated(session_id=...)
```

These are convenience reads; they must not create a second execution path.

## 5. Building blocks constructors

### 5.1 Skills

```python
SkillStore.from_dir("./skills")
SkillStore.from_dirs(["./skills/builtin", "./skills/user"])
```

### 5.2 Memory

```python
Memory.local(path="./.ariadne/memory")
Memory.in_memory()  # tests only
```

### 5.3 Tools

```python
registry = ToolRegistry()
registry.register(my_spec, handler)
registry.extend(ToolRegistry.builtins(include_sandbox=True))
```

### 5.4 Sandbox

```python
NullSandbox()
# later: LocalSandbox(...), DockerSandbox(...)
```

## 6. Host adapter guidance

### CLI

- Parse argv → `RunTurnCommand`
- Print text or stream events
- Exit non-zero on `failed`

### HTTP (optional later)

- Map request body → `RunTurnCommand`
- Map `TurnResult` / events → JSON or SSE
- Authentication is host concern; kernel remains callable without HTTP

### Notebook / library

- Use `Agent` façade directly
- Keep long-lived `Agent` instance per process when stores allow

## 7. What is not public API

- SQLAlchemy models
- Provider raw payloads as required input
- Internal ranking weights
- Sandbox container implementation details
- Any company pack / connector types

## 8. Versioning

Until 0.1.0:

- docs are normative
- breaking renames allowed with doc updates

From 0.1.0:

- semantic versioning on the `ariadne` package
- `AppError.code` treated as compatibility surface

## 9. Minimal success demo (acceptance)

A contributor can run:

```bash
ariadne run --session demo "create a short checklist for shipping docs"
```

and observe:

1. at least one model exchange
2. optional tool calls with traces
3. a final assistant text
4. artifacts under `.ariadne/` (memory/traces) when enabled
