"""Unit tests for kernel.tool_exchange (single-exchange invoke loop)."""

from __future__ import annotations

import asyncio
from typing import Any

from ariadne.errors import AriadneError, app_error
from ariadne.kernel.tool_exchange import invoke_tool_exchange
from ariadne.tools.registry import ToolContext, ToolRegistry, ToolSpec
from ariadne.types import SkillEvent


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", turn_id="t", sandbox=None)


def _registry_echo() -> ToolRegistry:
    reg = ToolRegistry()

    async def echo(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"echo": args.get("x")}

    async def boom(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        raise AriadneError(app_error("ARIADNE_TEST_BOOM", "nope", path="/x"))

    async def crash(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    reg.register(
        ToolSpec(
            name="echo",
            description="echo",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
            handler=echo,
            side_effect_level="none",
        )
    )
    reg.register(
        ToolSpec(
            name="boom",
            description="boom",
            parameters={"type": "object", "properties": {}},
            handler=boom,
            side_effect_level="none",
        )
    )
    reg.register(
        ToolSpec(
            name="crash",
            description="crash",
            parameters={"type": "object", "properties": {}},
            handler=crash,
            side_effect_level="none",
        )
    )
    return reg


def test_invoke_success_and_evidence() -> None:
    reg = _registry_echo()
    payload = [
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "echo", "arguments": '{"x":"hi"}'},
        }
    ]

    async def run() -> Any:
        return await invoke_tool_exchange(
            tools=reg,
            tool_calls_payload=payload,
            ctx=_ctx(),
            redact_traces=False,
            task_id="tid",
            step_id="sid",
            attempt_id="aid",
        )

    result = asyncio.run(run())
    assert len(result.traces) == 1
    assert result.traces[0].status == "completed"
    assert result.traces[0].output == {"echo": "hi"}
    assert result.traces[0].task_id == "tid"
    assert result.traces[0].attempt_id == "aid"
    assert result.tool_names == ["echo"]
    kinds = [e.kind for e in result.events]
    assert kinds == ["tool_started", "tool_completed"]
    assert len(result.appends) == 1
    assert result.appends[0].message["role"] == "tool"
    assert result.evidence_snippets and "hi" in result.evidence_snippets[0]


def test_invoke_ariadne_error_and_handler_error() -> None:
    reg = _registry_echo()
    payload = [
        {
            "id": "b1",
            "function": {"name": "boom", "arguments": "{}"},
        },
        {
            "id": "c1",
            "function": {"name": "crash", "arguments": "{}"},
        },
    ]

    result = asyncio.run(
        invoke_tool_exchange(
            tools=reg,
            tool_calls_payload=payload,
            ctx=_ctx(),
            redact_traces=False,
        )
    )
    assert len(result.traces) == 2
    assert result.traces[0].status == "failed"
    assert result.traces[0].error is not None
    assert result.traces[0].error.code == "ARIADNE_TEST_BOOM"
    assert result.traces[1].status == "failed"
    assert result.traces[1].error is not None
    assert result.traces[1].error.code == "ARIADNE_TOOL_HANDLER_ERROR"
    failed_events = [e for e in result.events if e.kind == "tool_completed"]
    assert all(e.data["status"] == "failed" for e in failed_events)


def test_invoke_invalid_args_not_object() -> None:
    reg = _registry_echo()
    payload = [
        {
            "id": "bad",
            "function": {"name": "echo", "arguments": '["not","object"]'},
        }
    ]
    result = asyncio.run(
        invoke_tool_exchange(
            tools=reg,
            tool_calls_payload=payload,
            ctx=_ctx(),
            redact_traces=False,
        )
    )
    assert result.traces[0].status == "failed"
    assert result.traces[0].error is not None
    assert result.traces[0].error.code == "ARIADNE_INVALID_TOOL_ARGS"


def test_adopt_skill_binds_when_attempt_present() -> None:
    reg = ToolRegistry()
    events: list[SkillEvent] = []

    async def adopt(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        ev = SkillEvent(kind="adopt", skill_name=str(args.get("name") or ""))
        events.append(ev)
        if ctx.skill_events is not None:
            ctx.skill_events.append(ev)
        return {"adopted": True}

    reg.register(
        ToolSpec(
            name="adopt_skill",
            description="adopt",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
            },
            handler=adopt,
            side_effect_level="none",
        )
    )
    bound: list[tuple[str, str, str]] = []

    def bind(event: SkillEvent, *, task_id: str, step_id: str, attempt_id: str) -> None:
        event.task_id = task_id
        event.step_id = step_id
        event.attempt_id = attempt_id
        bound.append((task_id, step_id, attempt_id))

    skill_events: list[SkillEvent] = []
    ctx = _ctx()
    ctx.skill_events = skill_events

    async def run_handler() -> Any:
        # invoke goes through registry; skill_events list is shared
        return await invoke_tool_exchange(
            tools=reg,
            tool_calls_payload=[
                {
                    "id": "a1",
                    "function": {
                        "name": "adopt_skill",
                        "arguments": '{"name":"s1"}',
                    },
                }
            ],
            ctx=ctx,
            redact_traces=False,
            task_id="T",
            step_id="S",
            attempt_id="A",
            skill_events=skill_events,
            bind_skill_event=bind,
        )

    result = asyncio.run(run_handler())
    assert result.traces[0].status == "completed"
    assert bound == [("T", "S", "A")]
    assert any(e.kind == "skill_event" for e in result.events)
    assert skill_events[0].attempt_id == "A"
