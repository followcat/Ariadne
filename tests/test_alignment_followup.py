"""Follow-up alignment: namespace protect, ToolSpec/CapabilitySpec,
schema-cost, field demotion, before_turn_id, memory worker.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.memory.transcript import TranscriptStore
from ariadne.memory.worker import MemoryWorker
from ariadne.skills.store import SkillStore, infer_namespace
from ariadne.tools import CapabilitySpec, ToolSpec, build_default_registry


def _skill_md(root: Path, name: str, body: str = "body") -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: desc for {name}\nkeywords: [{name}]\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_namespace_per_root_and_builtin_protect(tmp_path: Path) -> None:
    builtin = tmp_path / "skills" / "builtin"
    user = tmp_path / "skills" / "user"
    user.mkdir(parents=True)
    _skill_md(builtin, "shipped_skill")
    store = SkillStore.from_dirs(
        [builtin, user],
        user_root=user,
        namespaces=["builtin", "user"],
    )
    assert store.get("shipped_skill") is not None
    assert store.get("shipped_skill").namespace == "builtin"
    assert not store.is_writable("shipped_skill")
    with pytest.raises(AriadneError) as ei:
        store.manage(
            action="update",
            name="shipped_skill",
            description="hacked",
            body="nope",
        )
    assert ei.value.error.code == "ARIADNE_SKILL_INVALID"
    assert "read-only" in ei.value.error.message
    with pytest.raises(AriadneError):
        store.manage(action="delete", name="shipped_skill")
    # User skills remain writable.
    out = store.manage(
        action="create",
        name="my_skill",
        description="user owned",
        body="do things",
    )
    assert out["namespace"] == "user"
    assert store.get("my_skill").namespace == "user"


def test_infer_namespace() -> None:
    assert infer_namespace(Path("/x/skills/builtin")) == "builtin"
    assert infer_namespace(Path("/x/skills/user"), user_root=Path("/x/skills/user")) == "user"


def test_capability_spec_alias_and_tool_schema() -> None:
    assert CapabilitySpec is ToolSpec
    reg = build_default_registry(enable_deferred_demo=True)
    mem = reg.get("memory")
    assert mem is not None
    assert mem.catalog_phrase() == "durable curated memory"
    schema = mem.tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "memory"
    assert "parameters" in schema["function"]
    assert mem.schema_chars() == len(
        __import__("json").dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def test_schema_cost_deferred_lower_than_eager() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    deferred = reg.schema_cost_report(prefer_deferred=True)
    eager = reg.schema_cost_report(prefer_deferred=False)
    assert deferred["deferred_tool_count"] >= 2  # conversation_state, skill_manage, …
    assert "conversation_state" in deferred["deferred_names"]
    assert deferred["request_schema_chars"] < eager["request_schema_chars"]
    # Cost win must not delete tools from the catalog.
    assert "conversation_state" in reg.catalog_text()
    assert deferred["catalog_chars"] > 0
    # Correctness: deferred not callable until load.
    exp = reg.build_exposure(prefer_deferred=True)
    assert "conversation_state" not in exp.callable_function_names
    loaded = exp.load_exact(["conversation_state"])
    assert loaded and "conversation_state" in exp.callable_function_names


def test_field_level_demotion_prefers_current_value(tmp_path: Path) -> None:
    sem = SemanticIndex(tmp_path / "sem.json")
    # Stale chunk: mentions entity but old route value
    sem.index_turn(
        session_id="s1",
        turn_id="t_old",
        user_text="route was NORTH-10",
        assistant_text="noted NORTH-10 for record:R17",
        entity_ids=["record:R17"],
    )
    # Fresh chunk: current L2 value
    sem.index_turn(
        session_id="s1",
        turn_id="t_new",
        user_text="route updated to SOUTH-29",
        assistant_text="record:R17 is now SOUTH-29",
        entity_ids=["record:R17"],
    )
    auth = {
        "record:R17": {
            "route": {"value": "SOUTH-29", "source_turn_id": "t_new"},
        }
    }
    hits = sem.search(
        session_id="s1",
        query="what is the route for record R17",
        limit=5,
        authoritative_fields=auth,
    )
    assert hits
    # Current-value chunk should outrank stale (demoted) hit.
    assert hits[0]["turn_id"] == "t_new"
    stale = next(h for h in hits if h["turn_id"] == "t_old")
    fresh = next(h for h in hits if h["turn_id"] == "t_new")
    assert fresh["score"] > stale["score"]


def test_before_turn_id_filters_state_summary_semantic(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=state,
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
        hybrid_semantic=False,
    )
    mem.transcript.append(
        {"role": "user", "content": "first", "turn_id": "t1", "session_id": "s1"}
    )
    mem.transcript.append(
        {"role": "assistant", "content": "ok1", "turn_id": "t1", "session_id": "s1"}
    )
    mem.transcript.append(
        {"role": "user", "content": "second", "turn_id": "t2", "session_id": "s1"}
    )
    mem.transcript.append(
        {"role": "assistant", "content": "ok2", "turn_id": "t2", "session_id": "s1"}
    )
    state.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="route SOUTH-29 now",
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "record:R17",
                "type": "record",
                "evidence_quote": "SOUTH-29",
            },
            {
                "op": "set_attribute",
                "entity_id": "record:R17",
                "key": "route",
                "value": "SOUTH-29",
                "evidence_quote": "SOUTH-29",
            },
        ],
    )
    # Second turn overwrites attribute
    state.apply_ops(
        session_id="s1",
        source_turn_id="t2",
        evidence_text="route changed to NORTH-99",
        operations=[
            {
                "op": "set_attribute",
                "entity_id": "record:R17",
                "key": "route",
                "value": "NORTH-99",
                "evidence_quote": "NORTH-99",
            },
        ],
    )
    mem.summaries.put(session_id="s1", turn_id="t1", summary_text="set route SOUTH-29")
    mem.summaries.put(session_id="s1", turn_id="t2", summary_text="set route NORTH-99")
    mem.semantic.index_turn(
        session_id="s1",
        turn_id="t1",
        user_text="first",
        assistant_text="SOUTH-29",
    )
    mem.semantic.index_turn(
        session_id="s1",
        turn_id="t2",
        user_text="second",
        assistant_text="NORTH-99",
    )

    # Point-in-time as of before t2: only t1 attrs / summaries / semantic.
    text, summary = mem.build_context(
        session_id="s1", query="route", before_turn_id="t2"
    )
    assert "SOUTH-29" in text
    assert "NORTH-99" not in text
    assert "set route SOUTH-29" in text
    assert "set route NORTH-99" not in text
    pit_layers = [L for L in summary.layers if L.name == "conversation_state"]
    assert pit_layers and "before_turn_id:t2" in (pit_layers[0].notes or "")


def test_memory_worker_drains_summary_and_projection(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    projection = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=state,
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
        projection=projection,
    )
    mem.summaries.enqueue(
        session_id="s1", turn_id="t1", source_text="user talked about routes"
    )
    projection.enqueue(session_id="s1", turn_id="t1", evidence_text="evidence")
    assert mem.summaries.pending_count("s1") == 1
    assert projection.pending_lag("s1") == 1

    worker = MemoryWorker(memory=mem)

    async def go():
        return await worker.run_once()

    result = asyncio.run(go())
    assert result["summaries_processed"] == 1
    assert result["projection_count"] == 1
    assert mem.summaries.pending_count("s1") == 0
    assert projection.pending_lag("s1") == 0
    assert mem.summaries.list_ready("s1")
    assert projection.list_jobs(session_id="s1")[0]["status"] == "no_change"
