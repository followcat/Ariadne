from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from ..types import Message, Usage
from .base import ModelExchange, ModelStreamEvent


ScriptFn = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], dict[str, Any]]


@dataclass
class FakeModel:
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
            reasoning_content=str(msg.get("reasoning_content") or ""),
        )
        return ModelExchange(
            message=message,
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            raw={"choices": [{"message": msg}]},
        )

    async def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        exchange = await self.complete(
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
        )
        if exchange.message.reasoning_content:
            yield ModelStreamEvent(
                kind="thinking_delta", text=exchange.message.reasoning_content
            )
        if exchange.message.content:
            # yield in small chunks for stream tests
            text = exchange.message.content
            mid = max(1, len(text) // 2)
            yield ModelStreamEvent(kind="delta", text=text[:mid])
            if text[mid:]:
                yield ModelStreamEvent(kind="delta", text=text[mid:])
        yield ModelStreamEvent(kind="completed", exchange=exchange)
