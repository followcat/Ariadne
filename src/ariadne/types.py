from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .context.compiler import ContextAttribution
    from .tasks.models import TaskSummary


@dataclass(slots=True)
class AppError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retriable: bool = False


@dataclass(slots=True)
class ToolCallTrace:
    call_id: str
    name: str
    arguments: dict[str, Any]
    output: Any = None
    status: Literal["completed", "failed"] = "completed"
    error: AppError | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    schema_chars: int = 0
    task_id: str = ""
    step_id: str = ""
    attempt_id: str = ""


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(slots=True)
class LayerReport:
    name: str
    status: Literal["used", "skipped", "failed", "disabled", "stale_delta"]
    token_chars: int = 0
    item_ids: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass(slots=True)
class MemoryContextSummary:
    layers: list[LayerReport] = field(default_factory=list)
    curated_count: int = 0
    state_entity_count: int = 0
    recent_turn_count: int = 0


@dataclass(slots=True)
class MemoryContext:
    """Structured memory build result (MEMORY §4 Read API)."""

    system_text: str
    summary: MemoryContextSummary
    before_turn_id: str | None = None
    require_ready: bool = False


@dataclass(slots=True)
class SkillEvent:
    kind: Literal["search", "load", "index"]
    skill_name: str = ""
    detail: str = ""
    # sha256 hex prefix of skill body when load happened (audit / replay pins)
    content_digest: str = ""


@dataclass(slots=True)
class SchemaMetrics:
    exchange_index: int
    tool_count: int
    schema_chars: int
    catalog_chars: int
    deferred_count: int
    loaded_deferred: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    # Provider reasoning / chain-of-thought (not always present; not re-sent by default)
    reasoning_content: str = ""


@dataclass(slots=True)
class RunTurnCommand:
    session_id: str
    input: str | list[Message]
    user_id: str | None = None
    model: str | None = None
    stream: bool = False
    tool_loop_limit: int = 32
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TurnResult:
    turn_id: str
    status: Literal["completed", "failed", "needs_input"]
    text: str
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    skill_events: list[SkillEvent] = field(default_factory=list)
    memory: MemoryContextSummary = field(default_factory=MemoryContextSummary)
    usage: Usage = field(default_factory=Usage)
    error: AppError | None = None
    session_id: str = ""
    model: str = ""
    schema_metrics: list[SchemaMetrics] = field(default_factory=list)
    # skill name → content digest for skills loaded this turn (audit/replay)
    skill_pins: dict[str, str] = field(default_factory=dict)
    task: TaskSummary | None = None
    context_attributions: list[ContextAttribution] = field(default_factory=list)


# Streaming / host events
TurnEventKind = Literal[
    "turn_started",
    "model_delta",
    "model_thinking_delta",  # reasoning / chain-of-thought (UI may collapse when answer ready)
    "tool_started",
    "tool_completed",
    "skill_event",
    "memory_layer",
    "task_started",
    "task_replanned",
    "task_resumed",
    "task_step_started",
    "task_check_completed",
    "task_needs_input",
    "task_completed",
    "task_failed",
    "guard_finding",
    "turn_completed",
    "turn_failed",
]


@dataclass(slots=True)
class TurnEvent:
    kind: TurnEventKind
    data: dict[str, Any] = field(default_factory=dict)
