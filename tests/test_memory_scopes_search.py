"""Memory scopes + graded memory_search (design/memory-scopes.md, memory-search.md)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory import Memory
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.memory.transcript import TranscriptStore
from ariadne.tools.registry import ToolContext, build_default_registry


def test_user_id_mismatch_fastfails(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem", user_id="alice")
    with pytest.raises(AriadneError) as ei:
        mem.build_context(session_id="s1", query="x", user_id="bob")
    assert ei.value.error.code == "ARIADNE_CONFIG_INVALID"
    assert "user_id" in str(ei.value).lower() or "bob" in str(ei.value)


def test_user_scope_separate_store_cross_workspace(tmp_path: Path) -> None:
    """User curated at shared path; workspace stores stay isolated."""
    user_path = tmp_path / "user" / "curated.json"
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    mem_a = Memory.local(
        path=ws_a, user_id="local", user_curated_path=user_path
    )
    mem_b = Memory.local(
        path=ws_b, user_id="local", user_curated_path=user_path
    )
    mem_a.apply_curated(
        action="add",
        content="prefer dark theme",
        scope="user",
        session_id="s1",
        source_turn_id="t1",
    )
    mem_a.apply_curated(
        action="add",
        content="ws-a only fact",
        scope="workspace",
        session_id="s1",
    )
    text_b, summary_b = mem_b.build_context(session_id="s2", query="theme")
    assert "prefer dark theme" in text_b
    assert "ws-a only fact" not in text_b
    text_a, _ = mem_a.build_context(session_id="s1", query="fact")
    assert "ws-a only fact" in text_a


def test_workspace_curated_not_leaked_to_other_root(tmp_path: Path) -> None:
    mem_a = Memory.local(path=tmp_path / "a")
    mem_b = Memory.local(path=tmp_path / "b")
    mem_a.curated.apply(
        action="add",
        content="secret project alpha",
        scope="workspace",
        session_id="s1",
    )
    text_b, _ = mem_b.build_context(session_id="s1", query="project")
    assert "secret project alpha" not in text_b


def test_memory_search_fast_hits_grounded(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    mem.semantic.index_turn(
        session_id="s1",
        turn_id="turn-42",
        user_text="we decided on billing migration plan v3",
        assistant_text="ok, using plan v3 for billing",
    )
    result = asyncio.run(
        mem.memory_search(
            query="billing migration plan",
            session_id="s1",
            scope="session",
            mode="fast",
            limit=5,
        )
    )
    assert result["mode_used"] == "fast"
    assert result["hits"], "expected at least one hit"
    for hit in result["hits"]:
        assert hit["turn_id"] == "turn-42"
        assert hit["session_id"] == "s1"
        assert hit["snippet"]
        # snippet grounded in indexed source
        assert "billing" in hit["snippet"].lower() or "plan" in hit["snippet"].lower()


def test_memory_search_empty_corpus_no_invention(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    result = asyncio.run(
        mem.memory_search(
            query="nonexistent needle xyzzy",
            session_id="s1",
            scope="session",
            mode="fast",
        )
    )
    assert result["hits"] == []
    assert "empty" in (result.get("notes") or "")


def test_memory_search_cross_session_workspace_scope(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    mem.semantic.index_turn(
        session_id="sess-old",
        turn_id="t-old",
        user_text="library foobar replaced baz after drop",
        assistant_text="noted foobar",
    )
    # session scope must not see other sessions
    r_sess = asyncio.run(
        mem.memory_search(
            query="foobar library",
            session_id="sess-new",
            scope="session",
            mode="fast",
        )
    )
    assert all(h.get("session_id") != "sess-old" for h in r_sess["hits"]) or not r_sess["hits"]
    # workspace scope searches whole index
    r_ws = asyncio.run(
        mem.memory_search(
            query="foobar library",
            session_id="sess-new",
            scope="workspace",
            mode="fast",
        )
    )
    assert any(h.get("turn_id") == "t-old" for h in r_ws["hits"])


def test_memory_search_user_scope_curated(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    mem.apply_curated(
        action="add",
        content="timezone is Asia/Shanghai",
        scope="user",
        session_id="s1",
        source_turn_id="t-pref",
    )
    result = asyncio.run(
        mem.memory_search(
            query="timezone Shanghai",
            session_id="s1",
            scope="user",
            mode="fast",
        )
    )
    assert result["hits"]
    assert "Asia/Shanghai" in result["hits"][0]["snippet"]
    assert result["hits"][0]["evidence"]["source"] == "curated"


def test_projection_disabled_by_default_honest_layer(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    assert mem.projection is None
    _, summary = mem.build_context(session_id="s1", query="hello")
    layer = next(l for l in summary.layers if l.name == "conversation_state")
    assert layer.status == "disabled"
    assert "projection:disabled" in (layer.notes or "")


def test_projection_enabled_does_not_auto_no_change_without_worker(
    tmp_path: Path,
) -> None:
    """With projection queue present, jobs stay pending until a projector runs."""
    mem = Memory.local(path=tmp_path / "mem", enable_projection=True)
    assert mem.projection is not None
    job_id = mem.projection.enqueue(
        session_id="s1", turn_id="t1", evidence_text="created FOO"
    )
    assert job_id
    assert mem.projection.pending_lag("s1") == 1
    # No silent complete — lag remains until drain with a real projector
    jobs = mem.projection.list_jobs(session_id="s1")
    assert jobs[0]["status"] == "pending"


def test_curated_stable_ids_and_source_turn(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    a = mem.curated.apply(
        action="add",
        content="first entry",
        scope="user",
        session_id="s1",
        source_turn_id="turn-a",
    )
    b = mem.curated.apply(
        action="add",
        content="second entry",
        scope="user",
        session_id="s1",
        source_turn_id="turn-b",
    )
    id_a = a["entries"][0]["id"]
    id_b = b["entries"][1]["id"]
    assert id_a != id_b
    assert not str(id_a).startswith("e") or len(id_a) > 3
    mem.curated.apply(action="remove", entry_ref=id_a, scope="user", session_id="s1")
    remaining = mem.curated.apply(action="read", scope="user", session_id="s1")["entries"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == id_b  # not renumbered
    assert remaining[0]["source_turn_id"] == "turn-b"


def test_memory_search_tool_registered(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    reg = build_default_registry(memory=mem, skills=None)
    assert "memory_search" in reg.tools
    assert "memory" in reg.tools
    # schema includes workspace scope
    mem_params = reg.tools["memory"].parameters
    assert "workspace" in mem_params["properties"]["scope"]["enum"]


def test_memory_search_tool_invoke(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "mem")
    mem.semantic.index_turn(
        session_id="s1",
        turn_id="t9",
        user_text="needle alpha unique token",
        assistant_text="ack",
    )
    reg = build_default_registry(memory=mem, skills=None)
    ctx = ToolContext(session_id="s1", turn_id="t-now", sandbox=None, memory=mem)

    async def _run():
        return await reg.invoke(
            "memory_search",
            {"query": "needle alpha", "scope": "session", "mode": "fast"},
            ctx,
        )

    out = asyncio.run(_run())
    assert out["hits"]
    assert out["hits"][0]["turn_id"] == "t9"


def test_skill_digest_pins_on_load(tmp_path: Path) -> None:
    """load_skill records content_digest on SkillEvent for turn pins."""
    import hashlib
    from ariadne.skills.store import SkillStore

    skills_root = tmp_path / "skills-user"
    skill_dir = skills_root / "pin-demo"
    skill_dir.mkdir(parents=True)
    body = "# Pin demo\n\nDo the pin thing carefully.\n"
    (skill_dir / "SKILL.md").write_text(
        "---\nname: pin-demo\ndescription: pin demo skill\n---\n" + body,
        encoding="utf-8",
    )
    skills = SkillStore.from_dirs([skills_root], strict=False, user_root=skills_root)
    mem = Memory.local(path=tmp_path / "mem")
    reg = build_default_registry(memory=mem, skills=skills)
    events: list = []
    ctx = ToolContext(
        session_id="s1",
        turn_id="t1",
        sandbox=None,
        memory=mem,
        skills=skills,
        skill_events=events,
    )

    async def _run():
        return await reg.invoke("load_skill", {"name": "pin-demo"}, ctx)

    out = asyncio.run(_run())
    assert out["content_digest"]
    assert events and events[0].kind == "load"
    assert events[0].content_digest == out["content_digest"]
    # digest is of returned body
    expect = hashlib.sha256(out["body"].encode("utf-8")).hexdigest()[:16]
    assert out["content_digest"] == expect
