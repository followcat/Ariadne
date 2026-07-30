from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from ..errors import AriadneError, app_error

Disposition = Literal["included", "summarized", "dropped"]


def _serialized_chars(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@dataclass(slots=True)
class ContextBlock:
    source: str
    role: Literal["system", "user", "assistant", "tool"]
    content: Any
    reason: str
    score: float = 0.0
    required: bool = False
    trust: str = "untrusted"
    verbatim: bool = False
    tool_call_id: str | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class ContextAttribution:
    source: str
    reason: str
    score: float
    token_chars: int
    disposition: Disposition
    role: str
    trust: str
    required: bool = False
    verbatim: bool = False


@dataclass(slots=True)
class CompiledContext:
    messages: list[dict[str, Any]] = field(default_factory=list)
    attributions: list[ContextAttribution] = field(default_factory=list)
    total_chars: int = 0


@dataclass(slots=True)
class ContextCompiler:
    """Compile authoritative prompt blocks under one deterministic hard budget.

    Required/verbatim blocks are never clipped. Optional blocks compete by score;
    a string block may be visibly summarized by prefix clipping, otherwise it is
    dropped. Every decision is returned as an attribution record.
    """

    max_chars: int = 120_000
    min_summary_chars: int = 160

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "context max_chars must be positive")
            )
        if self.min_summary_chars < 32:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "context min_summary_chars must be at least 32",
                )
            )

    @staticmethod
    def _message(block: ContextBlock, content: Any) -> dict[str, Any]:
        message: dict[str, Any] = {"role": block.role, "content": content}
        if block.tool_call_id:
            message["tool_call_id"] = block.tool_call_id
        if block.name:
            message["name"] = block.name
        if block.tool_calls:
            message["tool_calls"] = block.tool_calls
        return message

    @classmethod
    def _block_chars(cls, block: ContextBlock, content: Any) -> int:
        return _serialized_chars(cls._message(block, content))

    @staticmethod
    def serialized_chars(value: Any) -> int:
        return _serialized_chars(value)

    def compile(
        self,
        blocks: list[ContextBlock],
        *,
        reserved_chars: int = 0,
    ) -> CompiledContext:
        reserved = max(0, int(reserved_chars))
        required_chars = sum(
            self._block_chars(block, block.content)
            for block in blocks
            if block.required
        )
        if required_chars + reserved > self.max_chars:
            required_sources = [block.source for block in blocks if block.required]
            raise AriadneError(
                app_error(
                    "ARIADNE_CONTEXT_BUDGET_EXCEEDED",
                    "required context does not fit the configured hard budget",
                    required_chars=required_chars,
                    reserved_chars=reserved,
                    max_chars=self.max_chars,
                    required_sources=required_sources,
                )
            )

        remaining = self.max_chars - required_chars - reserved
        selected: dict[int, tuple[Disposition, Any]] = {}
        optional = [
            (index, block)
            for index, block in enumerate(blocks)
            if not block.required
        ]
        for index, block in sorted(optional, key=lambda item: (-item[1].score, item[0])):
            size = self._block_chars(block, block.content)
            if size <= remaining:
                selected[index] = ("included", block.content)
                remaining -= size
                continue
            marker = f"\n[ariadne: {block.source} summarized by ContextCompiler]"
            empty_message_chars = self._block_chars(block, "")
            available_content_chars = max(0, remaining - empty_message_chars)
            if (
                isinstance(block.content, str)
                and not block.verbatim
                and available_content_chars
                >= max(self.min_summary_chars, len(marker))
            ):
                prefix_chars = max(0, available_content_chars - len(marker))
                summarized = block.content[:prefix_chars].rstrip() + marker
                while prefix_chars > 0 and self._block_chars(block, summarized) > remaining:
                    prefix_chars -= 1
                    summarized = block.content[:prefix_chars].rstrip() + marker
                if self._block_chars(block, summarized) <= remaining:
                    selected[index] = ("summarized", summarized)
                    remaining -= self._block_chars(block, summarized)
                    continue
            selected[index] = ("dropped", "")

        messages: list[dict[str, Any]] = []
        attributions: list[ContextAttribution] = []
        total = 0
        for index, block in enumerate(blocks):
            if block.required:
                disposition: Disposition = "included"
                content = block.content
            else:
                disposition, content = selected[index]
            chars = 0 if disposition == "dropped" else self._block_chars(block, content)
            if disposition != "dropped":
                messages.append(self._message(block, content))
                total += chars
            attributions.append(
                ContextAttribution(
                    source=block.source,
                    reason=block.reason,
                    score=block.score,
                    token_chars=chars,
                    disposition=disposition,
                    role=block.role,
                    trust=block.trust,
                    required=block.required,
                    verbatim=block.verbatim,
                )
            )
        return CompiledContext(messages=messages, attributions=attributions, total_chars=total)

    def append_required(
        self,
        *,
        messages: list[dict[str, Any]],
        attributions: list[ContextAttribution],
        block: ContextBlock,
        reserved_chars: int = 0,
    ) -> None:
        if not block.required:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONTEXT_INVALID",
                    "append_required only accepts required context blocks",
                    source=block.source,
                )
            )
        current = sum(_serialized_chars(message) for message in messages)
        added = self._block_chars(block, block.content)
        reserved = max(0, int(reserved_chars))
        if current + added + reserved > self.max_chars:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONTEXT_BUDGET_EXCEEDED",
                    "required dynamic evidence does not fit the configured hard budget",
                    source=block.source,
                    current_chars=current,
                    added_chars=added,
                    reserved_chars=reserved,
                    max_chars=self.max_chars,
                )
            )
        messages.append(self._message(block, block.content))
        attributions.append(
            ContextAttribution(
                source=block.source,
                reason=block.reason,
                score=block.score,
                token_chars=added,
                disposition="included",
                role=block.role,
                trust=block.trust,
                required=True,
                verbatim=block.verbatim,
            )
        )

    def ensure_request_fits(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> None:
        message_chars = sum(_serialized_chars(message) for message in messages)
        tool_chars = _serialized_chars(tools) if tools else 0
        if message_chars + tool_chars > self.max_chars:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONTEXT_BUDGET_EXCEEDED",
                    "serialized model request exceeds the configured hard budget",
                    message_chars=message_chars,
                    tool_schema_chars=tool_chars,
                    max_chars=self.max_chars,
                )
            )
