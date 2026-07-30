"""PUBLIC_API surface tests: RunTurnCommand, TurnResult.messages, Memory constructors."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ariadne import Agent, Memory, RunTurnCommand
from ariadne.kernel.turn import TurnApplication
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _script(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {"content": "ack: done"}


def _agent(tmp_path: Path, *, prestart: bool = False) -> Agent:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    turn_app = TurnApplication(
        model=FakeModel(script=_script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        sandbox_prestart=prestart,
    )
    return Agent(turn_app=turn_app, session_id="pub", model="fake-model")


def test_run_turn_command_roundtrip(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    async def run() -> None:
        command = RunTurnCommand(
            session_id="pub",
            input="hello kernel",
            user_id="u1",
            tool_loop_limit=4,
            metadata={"source": "test"},
        )
        result = await agent.run(command)
        assert result.status == "completed"
        assert result.text == "ack: done"
        # TurnResult.messages carries the public conversation (no system assembly)
        roles = [m.role for m in result.messages]
        assert "system" not in roles
        assert roles[0] == "user"
        assert result.messages[0].content == "hello kernel"
        assert roles[-1] == "assistant"
        assert result.messages[-1].content == "ack: done"

    asyncio.run(run())


def test_memory_constructors(tmp_path: Path) -> None:
    memory = Memory.local(path=tmp_path / "mem")
    memory.curated.apply(
        action="add", content="prefer tables over prose", scope="user", session_id="s1"
    )
    text, summary = memory.build_context(session_id="s1", query="formatting")
    assert "prefer tables over prose" in text
    curated = memory.get_curated(session_id="s1")
    assert [e["content"] for e in curated["user"]] == ["prefer tables over prose"]

    disposable = Memory.in_memory()
    text2, _ = disposable.build_context(session_id="s2", query="nothing")
    assert isinstance(text2, str)


def test_agent_inspection_helpers(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    async def run() -> None:
        tools = await agent.list_tools()
        names = {t["name"] for t in tools}
        assert {"sandbox_exec", "memory", "memory_search", "search_skills"} <= names
        skills = await agent.list_skills()
        assert skills == []
        agent.turn_app.memory.curated.apply(
            action="add", content="likes coffee", scope="user", session_id="pub"
        )
        curated = await agent.get_curated()
        assert curated["user"][0]["content"] == "likes coffee"

    asyncio.run(run())


def test_last_good_plus_delta_read_mode(tmp_path: Path) -> None:
    memory = Memory.local(path=tmp_path / "mem")
    session = "s-delta"
    # turn t1 projected into state (last-good)
    memory.transcript.append({"role": "user", "content": "route is NORTH", "turn_id": "t1"})
    memory.transcript.append({"role": "assistant", "content": "noted NORTH", "turn_id": "t1"})
    memory.state.apply_ops(
        session_id=session,
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "route",
                "evidence_quote": "route is NORTH",
            },
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "NORTH",
                "evidence_quote": "route is NORTH",
            },
        ],
        source_turn_id="t1",
        evidence_text="route is NORTH",
    )
    # projector lags: t2 raw turns exist beyond the watermark
    memory.transcript.append({"role": "user", "content": "change route to SOUTH", "turn_id": "t2"})
    memory.transcript.append({"role": "assistant", "content": "route updated to SOUTH", "turn_id": "t2"})

    text, summary = memory.build_context(session_id=session, query="which route?")
    # last-good state is still rendered authoritatively
    assert "[CONVERSATION_STATE: AUTHORITATIVE]" in text
    assert "NORTH" in text
    # and the newer-than-state delta is rendered with precedence marker
    assert "[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]" in text
    assert "SOUTH" in text
    delta_layers = [l for l in summary.layers if l.name == "state_delta"]
    assert delta_layers and delta_layers[0].status == "stale_delta"

    # once projection catches up (watermark at t2), the delta disappears
    memory.state.apply_ops(
        session_id=session,
        operations=[
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "SOUTH",
                "evidence_quote": "route updated to SOUTH",
            }
        ],
        source_turn_id="t2",
        evidence_text="change route to SOUTH\nroute updated to SOUTH",
    )
    text2, summary2 = memory.build_context(session_id=session, query="which route?")
    assert "[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]" not in text2
    assert all(l.name != "state_delta" for l in summary2.layers)


def test_sandbox_prestart_turn(tmp_path: Path) -> None:
    (tmp_path / "pre").mkdir()
    agent = _agent(tmp_path / "pre", prestart=True)

    async def run() -> None:
        result = await agent.run("hello with prestart", session_id="pub")
        assert result.status == "completed"
        assert result.text == "ack: done"

    asyncio.run(run())
