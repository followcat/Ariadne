"""Turn lifecycle regressions: sandbox cleanup on every failure path."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError, app_error
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _script(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"content": "ok"}


class FailingMemory(Memory):
    async def build_context_async(self, *, session_id: str, query: str, user_id: str | None = None):
        raise AriadneError(app_error("ARIADNE_MEMORY_NOT_READY", "projection incomplete"))


def test_sandbox_closed_when_memory_build_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = FailingMemory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    app = TurnApplication(
        model=FakeModel(script=_script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
    )

    async def run() -> None:
        with pytest.raises(AriadneError) as excinfo:
            async for _ in app.run_events(prompt="hi", session_id="leak"):
                pass
        assert excinfo.value.error.code == "ARIADNE_MEMORY_NOT_READY"

    asyncio.run(run())
    # per_turn scratch must not leak when the turn dies before the tool loop
    leftover = list((tmp_path / "data" / "sandbox").glob("*/session"))
    assert leftover == [], f"leaked sandbox session dirs: {leftover}"


def test_sandbox_closed_on_cancellation(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    app = TurnApplication(
        model=FakeModel(script=_script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
    )

    async def run() -> None:
        gen = app.run_events(prompt="hi", session_id="cancel")
        async for event in gen:
            if event.kind == "turn_started":
                # host abandons the stream before the first model exchange
                await gen.aclose()
                break

    asyncio.run(run())
    leftover = list((tmp_path / "data" / "sandbox").glob("*/session"))
    assert leftover == [], f"leaked sandbox session dirs: {leftover}"


def test_empty_content_recovers_from_reasoning(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)

    def script(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        return {
            "content": "",
            "reasoning_content": "I should add drawBird() to crayon.js and write the file.",
        }

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        guardrails_enabled=False,
    )

    async def run() -> str:
        result = await app.run(prompt="draw bird", session_id="empty-r")
        return result.text

    text = asyncio.run(run())
    assert "drawBird" in text or "思考摘要" in text
    assert text.strip()


def test_empty_content_with_tools_nudge(tmp_path: Path) -> None:
    import json

    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "a.txt").write_text("x\n", encoding="utf-8")
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    step = {"n": 0}

    def script(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        n = step["n"]
        step["n"] += 1
        if n == 0:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "sandbox_read_file",
                            "arguments": json.dumps({"path": "/workspace/a.txt"}),
                        },
                    }
                ],
            }
        return {"content": "", "reasoning_content": ""}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        guardrails_enabled=False,
    )

    async def run() -> str:
        result = await app.run(prompt="read then empty", session_id="empty-t")
        return result.text

    text = asyncio.run(run())
    assert "sandbox_read_file" in text or "翻过" in text
    assert "文件" in text or "接着改" in text or "保存" in text
