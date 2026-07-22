from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from ..types import Message, Usage


@dataclass(slots=True)
class ModelExchange:
    message: Message
    usage: Usage
    raw: dict[str, Any]


@dataclass(slots=True)
class ModelStreamEvent:
    kind: str  # delta | thinking_delta | tool_call_delta | completed
    text: str = ""
    exchange: ModelExchange | None = None


class ModelPort(Protocol):
    model: str

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 8192,
        model: str | None = None,
    ) -> ModelExchange: ...

    async def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 8192,
        model: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]: ...
