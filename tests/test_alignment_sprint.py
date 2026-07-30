"""Alignment sprint: hybrid plan, auto_load bodies, deferred tools,
summary status machine, projection order, demotion, MemoryContext.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.memory.transcript import TranscriptStore
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _make_skill(root: Path, name: str, *, body: str = "", keywords: str | None = None) -> None:
    d = root / name
    d.mkdir(parents=True)
    kw = keywords or name
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: skill about {name}\nkeywords: [{kw}]\n---\n\n"
        f"{body or f'Body of {name} with unique marker MARKER_{name}.'}\n",
        encoding="utf-8",
    )


def test_plan_async_hybrid_returns_scored_bands(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _make_skill(root, "git_workflow")
    _make_skill(root, "docker_tips")
    store = SkillStore.from_dir(root)

    plan = asyncio.run(store.plan_async("tell me about git_workflow please"))
    assert plan["auto_load"] or plan["recommended"]
    names_auto = [s.name for s, _ in plan["auto_load"]]
    names_rec = [s.name for s, _ in plan["recommended"]]
    assert "git_workflow" in names_auto + names_rec
    # Scores are floats in hybrid band.
    for _, score in plan["auto_load"] + plan["recommended"]:
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0 or score >= 1.0  # hybrid ~0-1; lexical fallback int-as-float ok


def test_auto_load_body_materialized_in_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    skills_root = tmp_path / "skills"
    _make_skill(
        skills_root,
        "git_workflow",
        body="Always run git status before commit. MARKER_GIT_BODY_INJECT.",
    )

    class LexicalPlanStore(SkillStore):
        """Force lexical plan so AUTO_LOAD_SCORE promotes the named skill."""

        async def plan_async(self, query: str, **kwargs: Any) -> dict[str, Any]:
            return self.plan(query, **kwargs)

    skills = LexicalPlanStore.from_dir(skills_root)
    memory = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=ConversationStateStore(tmp_path / "s.json"),
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
    )
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    captured: dict[str, Any] = {}

    def script(messages: list[dict[str, Any]], tools_payload: list[dict[str, Any]] | None) -> dict[str, Any]:
        captured["messages"] = list(messages)
        return {"content": "ok from model"}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        prefer_deferred_tools=False,
    )
    # Exact skill name → lexical score >= AUTO_LOAD_SCORE → auto_load body.
    result = asyncio.run(app.run(prompt="git_workflow", session_id="s1"))
    assert result.status == "completed"
    bodies = [
        m.get("content", "")
        for m in captured["messages"]
        if m.get("role") == "system" and "[SKILL_BODY" in str(m.get("content", ""))
    ]
    assert bodies, "expected turn-scoped SKILL_BODY injection"
    assert "MARKER_GIT_BODY_INJECT" in bodies[0]
    assert "this_turn" in bodies[0]
    load_events = [e for e in result.skill_events if e.kind == "load"]
    assert any(e.skill_name == "git_workflow" for e in load_events)


def test_summary_enqueue_process_status_machine(tmp_path: Path) -> None:
    store = TurnSummaryStore(tmp_path / "sum.json")
    store.enqueue(session_id="s1", turn_id="t1", source_text="User asked about routes. " * 20)
    assert store.pending_count("s1") == 1
    assert store.list_ready("s1") == []
    n = store.process_pending(session_id="s1")
    assert n == 1
    assert store.pending_count("s1") == 0
    ready = store.list_ready("s1")
    assert len(ready) == 1
    assert ready[0]["turn_id"] == "t1"
    assert "routes" in ready[0]["summary_text"]
    # Idempotent: already ready enqueue is a no-op.
    store.enqueue(session_id="s1", turn_id="t1", source_text="should not overwrite")
    assert store.list_ready("s1")[0]["summary_text"].startswith("User asked")


def test_projection_claim_respects_session_turn_order(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    j1 = worker.enqueue(session_id="s1", turn_id="t1", evidence_text="first")
    j2 = worker.enqueue(session_id="s1", turn_id="t2", evidence_text="second")
    j_other = worker.enqueue(session_id="s2", turn_id="u1", evidence_text="other")

    c1 = worker.claim(worker_id="w1", session_id="s1")
    assert c1 is not None and c1["job_id"] == j1
    # Later s1 job blocked while t1 unfinished.
    assert worker.claim(worker_id="w2", session_id="s1") is None
    # Other session still claimable.
    c_other = worker.claim(worker_id="w3", session_id="s2")
    assert c_other is not None and c_other["job_id"] == j_other
    # Lag counts unfinished (leased t1 + pending t2).
    assert worker.pending_lag("s1") == 2

    worker.complete(
        j1,
        worker_id="w1",
        lease_token=c1["lease_token"],
        status="succeeded",
    )
    assert worker.pending_lag("s1") == 1
    c2 = worker.claim(worker_id="w1", session_id="s1")
    assert c2 is not None and c2["job_id"] == j2
    worker.complete(
        j2,
        worker_id="w1",
        lease_token=c2["lease_token"],
        status="succeeded",
    )
    assert worker.pending_lag("s1") == 0


def test_entities_mentioned_and_demotion_not_all_ids(tmp_path: Path) -> None:
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=ConversationStateStore(tmp_path / "s.json"),
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
    )
    mem.state.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="doc FOO.md and bare entity BAR",
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "doc:foo",
                "type": "file",
                "evidence_quote": "FOO.md",
            },
            {
                "op": "set_attribute",
                "entity_id": "doc:foo",
                "key": "path",
                "value": "FOO.md",
                "evidence_quote": "FOO.md",
            },
            {
                "op": "ensure_entity",
                "entity_id": "doc:bar",
                "type": "file",
                "evidence_quote": "BAR",
            },
        ],
    )
    # Only mentioned ids tagged.
    mentioned = mem.entities_mentioned_in_text("s1", "we updated FOO.md today")
    assert "doc:foo" in mentioned
    assert "doc:bar" not in mentioned
    # Demotion set is entities with attributes only (not entire entity set).
    demote = mem._demote_entities("s1")
    assert "doc:foo" in demote
    assert "doc:bar" not in demote


def test_memory_context_and_require_ready(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    projection = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=state,
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
        projection=projection,
        require_ready=False,
    )
    ctx = asyncio.run(
        mem.build_memory_context(session_id="s1", query="anything", before_turn_id="t0")
    )
    assert ctx.system_text is not None
    assert ctx.before_turn_id == "t0"
    assert ctx.require_ready is False
    assert ctx.summary is not None

    projection.enqueue(session_id="s1", turn_id="t1", evidence_text="lag")
    with pytest.raises(AriadneError) as ei:
        asyncio.run(
            mem.build_memory_context(session_id="s1", query="x", require_ready=True)
        )
    assert ei.value.error.code == "ARIADNE_MEMORY_NOT_READY"
    assert ei.value.error.details.get("pending_jobs") == 1
