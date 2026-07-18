"""CLI render_json covers the full TurnResult surface."""

from __future__ import annotations

import json

from ariadne.cli.render import render_json
from ariadne.types import Message, ToolCallTrace, TurnResult, Usage


def test_render_json_includes_messages() -> None:
    result = TurnResult(
        turn_id="t1",
        status="completed",
        text="done",
        messages=[
            Message(role="user", content="hi"),
            Message(role="assistant", content="done"),
        ],
        tool_calls=[ToolCallTrace(call_id="c1", name="sandbox_exec", arguments={"cmd": "ls"})],
        usage=Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        session_id="s1",
        model="m",
    )
    payload = json.loads(render_json(result))
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert payload["messages"][0]["content"] == "hi"
    assert payload["tool_calls"][0]["name"] == "sandbox_exec"
    assert payload["usage"]["total_tokens"] == 3
