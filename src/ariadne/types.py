from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


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
class SkillEvent:
    kind: Literal["search", "load", "index"]
    skill_name: str = ""
    detail: str = ""


@dataclass(slots=True)
class TurnResult:
    turn_id: str
    status: Literal["completed", "failed", "needs_input"]
    text: str
    tool_calls: list[ToolCallTrace] = field(default_factory=list)
    skill_events: list[SkillEvent] = field(default_factory=list)
    memory: MemoryContextSummary = field(default_factory=MemoryContextSummary)
    usage: Usage = field(default_factory=Usage)
    error: AppError | None = None
    session_id: str = ""
    model: str = ""


@dataclass(slots=True)
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
