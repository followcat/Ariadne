"""Reasoning / thinking extraction and turn events."""

from __future__ import annotations

import asyncio
from typing import Any

from ariadne.model.openai_chat import (
    OpenAIChatModel,
    _ThinkStreamSplitter,
    _split_think_tags,
)
from ariadne.model.base import ModelExchange, ModelStreamEvent
from ariadne.types import Message, Usage


def test_split_think_tags() -> None:
    vis, reason = _split_think_tags(
        "Hello <think>secret plan</think> world <thinking>more</thinking>!"
    )
    assert "Hello" in vis and "world" in vis
    assert "secret plan" in reason and "more" in reason
    assert "<think" not in vis.lower()


def test_think_stream_splitter_incremental() -> None:
    sp = _ThinkStreamSplitter()
    parts: list[tuple[str, str]] = []
    for piece in ["Hi <th", "ink>a", "bc</thi", "nk> end"]:
        parts.extend(sp.feed(piece))
    parts.extend(sp.flush())
    think = "".join(t for c, t in parts if c == "think")
    content = "".join(t for c, t in parts if c == "content")
    assert "abc" in think
    assert "Hi" in content and "end" in content


def test_extract_reasoning_fields() -> None:
    assert OpenAIChatModel._extract_reasoning({"reasoning_content": "r1"}) == "r1"
    assert OpenAIChatModel._extract_reasoning({"thinking": "r2"}) == "r2"
    assert OpenAIChatModel._extract_reasoning({"reasoning": {"text": "r3"}}) == "r3"


def test_turn_emits_thinking_delta() -> None:
    from pathlib import Path

    from ariadne.kernel.turn import TurnApplication
    from ariadne.memory.curated import CuratedStore
    from ariadne.memory.facade import MemoryFacade
    from ariadne.memory.semantic import SemanticIndex
    from ariadne.memory.state import ConversationStateStore
    from ariadne.memory.summary import TurnSummaryStore
    from ariadne.memory.transcript import TranscriptStore
    from ariadne.sandbox.local import LocalWorkdirSandbox
    from ariadne.skills.store import SkillStore
    from ariadne.tools.registry import build_default_registry

    class ThinkingModel:
        model = "fake-think"

        async def complete(self, **kwargs: Any) -> ModelExchange:
            return ModelExchange(
                message=Message(
                    role="assistant",
                    content="final answer",
                    reasoning_content="I should check tools",
                ),
                usage=Usage(),
                raw={},
            )

        async def stream(self, **kwargs: Any):
            yield ModelStreamEvent(kind="thinking_delta", text="step one…")
            yield ModelStreamEvent(kind="thinking_delta", text=" step two")
            yield ModelStreamEvent(kind="delta", text="final answer")
            yield ModelStreamEvent(
                kind="completed",
                exchange=ModelExchange(
                    message=Message(
                        role="assistant",
                        content="final answer",
                        reasoning_content="step one… step two",
                    ),
                    usage=Usage(),
                    raw={},
                ),
            )

    # minimal app
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp / "t.jsonl"),
        curated=CuratedStore(tmp / "c.json"),
        state=ConversationStateStore(tmp / "s.json"),
        summaries=TurnSummaryStore(tmp / "sum.json"),
        semantic=SemanticIndex(tmp / "sem.json"),
        hybrid_semantic=False,
    )
    skills = SkillStore({})
    tools = build_default_registry(memory=mem, skills=skills, enable_deferred_demo=False)
    app = TurnApplication(
        model=ThinkingModel(),
        tools=tools,
        memory=mem,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=tmp / "ws", data_dir=tmp / "data"),
        stream_model=True,
        prefer_deferred_tools=False,
    )
    (tmp / "ws").mkdir(parents=True, exist_ok=True)

    async def collect():
        events = []
        async for ev in app.run_events(prompt="hi", session_id="s1"):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    kinds = [e.kind for e in events]
    assert "model_thinking_delta" in kinds
    assert "model_delta" in kinds
    think = "".join(
        e.data.get("text", "") for e in events if e.kind == "model_thinking_delta"
    )
    assert "step one" in think and "step two" in think
    answer = "".join(e.data.get("text", "") for e in events if e.kind == "model_delta")
    assert "final answer" in answer
