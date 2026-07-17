from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..types import Message, Usage
from .base import ModelExchange


ScriptFn = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]]


@dataclass
class FakeModel:
    """Deterministic model for offline tests.

    `script` receives (messages, tools) and returns an OpenAI-like message dict:
    {"content": str|None, "tool_calls": optional}
    """

    script: ScriptFn
    model: str = "fake-model"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> ModelExchange:
        self.calls.append({"messages": messages, "tools": tools})
        msg = self.script(messages, tools)
        message = Message(
            role="assistant",
            content=str(msg.get("content") or ""),
            tool_calls=msg.get("tool_calls"),
        )
        return ModelExchange(
            message=message,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            raw={"choices": [{"message": msg}]},
        )
