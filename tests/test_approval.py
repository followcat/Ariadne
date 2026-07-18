"""Approval hook: kernel deny path + host policy modes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.cli.approval import WRITE_TOOLS, make_approval_hook
from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolRegistry, build_default_registry


def _exec_call(cmd: str) -> dict[str, Any]:
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "sandbox_exec", "arguments": json.dumps({"cmd": cmd})},
    }


def _app(tmp_path: Path, *, hook) -> TurnApplication:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)

    def script(messages: list[dict[str, Any]], tools_payload: list[dict[str, Any]] | None) -> dict[str, Any]:
        if not any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [_exec_call("echo hi > f.txt")]}
        return {"content": "done"}

    return TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        approval_hook=hook,
    )


def test_denied_tool_becomes_error_result_not_turn_failure(tmp_path: Path) -> None:
    app = _app(tmp_path, hook=lambda name, args: False)

    async def run():
        return await app.run(prompt="make a file", session_id="s1")

    result = asyncio.run(run())
    assert result.status == "completed", "model recovers after denial"
    trace = result.tool_calls[0]
    assert trace.status == "failed"
    assert trace.error is not None and trace.error.code == "ARIADNE_TOOL_DENIED"
    assert not (tmp_path / "proj" / "f.txt").exists(), "denied exec must not run"


def test_allowed_hook_lets_tool_run(tmp_path: Path) -> None:
    app = _app(tmp_path, hook=lambda name, args: True)
    result = asyncio.run(app.run(prompt="make a file", session_id="s1"))
    assert result.status == "completed"
    assert result.tool_calls[0].status == "completed"
    assert (tmp_path / "proj" / "f.txt").exists()


def test_readonly_mode_denies_writes_allows_reads() -> None:
    hook = make_approval_hook("readonly")
    assert hook is not None
    for name in WRITE_TOOLS:
        assert hook(name, {}) is False, name
    for name in ("sandbox_read_file", "memory", "conversation_state", "search_skills", "tool_search"):
        assert hook(name, {}) is True, name


def test_on_request_asks_and_honors_answer() -> None:
    answers = iter([True, False])
    hook = make_approval_hook("on-request", confirm=lambda q: next(answers))
    assert hook is not None
    assert hook("sandbox_exec", {"cmd": "rm x"}) is True
    assert hook("sandbox_edit_file", {"path": "a"}) is False
    # reads never ask
    assert hook("sandbox_read_file", {}) is True


def test_auto_mode_has_no_hook() -> None:
    assert make_approval_hook("auto") is None


def test_unknown_mode_fastfails() -> None:
    try:
        make_approval_hook("yolo")
    except AriadneError as exc:
        assert exc.error.code == "ARIADNE_CONFIG_INVALID"
    else:
        raise AssertionError("expected fastfail")


def test_registry_invoke_checks_hook() -> None:
    registry = ToolRegistry.builtins(include_sandbox=True, enable_deferred_demo=False)
    from ariadne.tools.registry import ToolContext

    ctx = ToolContext(
        session_id="s",
        turn_id="t",
        sandbox=None,
        approval_hook=lambda name, args: False,
    )

    async def run() -> None:
        try:
            await registry.invoke("sandbox_exec", {"cmd": "echo hi"}, ctx)
        except AriadneError as exc:
            assert exc.error.code == "ARIADNE_TOOL_DENIED"
        else:
            raise AssertionError("expected denial")

    asyncio.run(run())
