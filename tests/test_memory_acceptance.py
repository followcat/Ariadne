"""Memory acceptance scenarios from docs/design/memory-v1.md §12 + §8."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory import Memory


def test_durable_preference_cross_session(tmp_path: Path) -> None:
    """Scenario 1: durable pref set in one session is visible in another (L3)."""
    memory = Memory.local(path=tmp_path / "mem")
    memory.curated.apply(
        action="add", content="prefer tables over prose", scope="user", session_id="s1"
    )
    text, _ = memory.build_context(session_id="s2-new-session", query="formatting")
    assert "prefer tables over prose" in text


def test_curated_update_and_forget(tmp_path: Path) -> None:
    """Scenario: update replaces, forget removes — no resurrection."""
    memory = Memory.local(path=tmp_path / "mem")
    added = memory.curated.apply(
        action="add", content="editor is vim", scope="user", session_id="s1"
    )
    eid = str(added["entries"][0]["id"])
    memory.curated.apply(
        action="update",
        content="editor is neovim",
        entry_ref=eid,
        scope="user",
        session_id="s1",
    )
    entries = memory.get_curated(session_id="s1")["user"]
    assert [e["content"] for e in entries] == ["editor is neovim"]
    assert entries[0]["id"] == eid  # stable id after update
    memory.curated.apply(action="remove", entry_ref=eid, scope="user", session_id="s1")
    text, _ = memory.build_context(session_id="s1", query="editor")
    assert "vim" not in text


def test_multi_entity_state_binding(tmp_path: Path) -> None:
    """Scenario 2 (variant): two entities keep separate attributes; status filters render."""
    memory = Memory.local(path=tmp_path / "mem")
    evidence = "task alpha is open, task beta is done"
    memory.state.apply_ops(
        session_id="s1",
        operations=[
            {"op": "ensure_entity", "entity_id": "alpha", "type": "task", "evidence_quote": "task alpha is open"},
            {"op": "set_status", "entity_id": "alpha", "status": "active", "evidence_quote": "task alpha is open"},
            {"op": "ensure_entity", "entity_id": "beta", "type": "task", "evidence_quote": "task beta is done"},
            {"op": "set_status", "entity_id": "beta", "status": "done", "evidence_quote": "task beta is done"},
        ],
        source_turn_id="t1",
        evidence_text=evidence,
    )
    state = memory.state.get("s1")
    assert state["entities"]["alpha"]["status"] == "active"
    assert state["entities"]["beta"]["status"] == "done"
    text, count = memory.state.render("s1")
    assert count == 2 and "alpha" in text and "beta" in text


def test_session_isolation(tmp_path: Path) -> None:
    """Scenario: session state and session-scope curated do not leak across sessions."""
    memory = Memory.local(path=tmp_path / "mem")
    evidence = "secret plan X"
    memory.state.apply_ops(
        session_id="sess-a",
        operations=[
            {"op": "ensure_entity", "entity_id": "plan", "evidence_quote": "secret plan X"},
            {"op": "set_attribute", "entity_id": "plan", "key": "name", "value": "X", "evidence_quote": "secret plan X"},
        ],
        source_turn_id="t1",
        evidence_text=evidence,
    )
    memory.curated.apply(
        action="add", content="session-only note", scope="session", session_id="sess-a"
    )
    text_b, summary_b = memory.build_context(session_id="sess-b", query="plan")
    assert "secret plan X" not in text_b
    assert "session-only note" not in text_b
    assert summary_b.state_entity_count == 0


def test_state_versions_and_cas(tmp_path: Path) -> None:
    """Append-only StateVersion history + CAS parent mismatch fastfails."""
    memory = Memory.local(path=tmp_path / "mem")
    r1 = memory.state.apply_ops(
        session_id="s1",
        operations=[{"op": "ensure_entity", "entity_id": "a", "evidence_quote": "make a"}],
        source_turn_id="t1",
        evidence_text="make a",
    )
    assert r1["version"] == 1 and r1["parent_version"] == 0
    r2 = memory.state.apply_ops(
        session_id="s1",
        operations=[{"op": "ensure_entity", "entity_id": "b", "evidence_quote": "make b"}],
        source_turn_id="t2",
        evidence_text="make b",
        expected_parent_version=1,
    )
    assert r2["version"] == 2
    versions = memory.state.list_versions("s1")
    assert [v["version"] for v in versions] == [1, 2]
    with pytest.raises(AriadneError) as excinfo:
        memory.state.apply_ops(
            session_id="s1",
            operations=[{"op": "ensure_entity", "entity_id": "c", "evidence_quote": "make c"}],
            source_turn_id="t3",
            evidence_text="make c",
            expected_parent_version=1,  # stale parent
        )
    assert excinfo.value.error.code == "ARIADNE_MEMORY_NOT_READY"


def test_relation_and_collection_move_ops(tmp_path: Path) -> None:
    memory = Memory.local(path=tmp_path / "mem")
    evidence = "alpha blocks beta, order is beta then alpha"
    memory.state.apply_ops(
        session_id="s1",
        operations=[
            {"op": "ensure_entity", "entity_id": "alpha", "evidence_quote": "alpha blocks beta"},
            {"op": "ensure_entity", "entity_id": "beta", "evidence_quote": "alpha blocks beta"},
            {"op": "set_relation", "relation": "blocks", "from": "alpha", "to": "beta", "evidence_quote": "alpha blocks beta"},
            {"op": "ensure_collection", "name": "todos", "evidence_quote": "order is beta then alpha"},
            {"op": "collection_append", "name": "todos", "member": "alpha", "evidence_quote": "order is beta then alpha"},
            {"op": "collection_append", "name": "todos", "member": "beta", "evidence_quote": "order is beta then alpha"},
            {"op": "collection_move", "name": "todos", "member": "beta", "to_index": 0, "evidence_quote": "order is beta then alpha"},
        ],
        source_turn_id="t1",
        evidence_text=evidence,
    )
    state = memory.state.get("s1")
    assert state["relations"]["blocks"] == [{"from": "alpha", "to": "beta"}]
    assert state["collections"]["todos"]["members"] == ["beta", "alpha"]
    text, _ = memory.state.render("s1")
    assert "relation blocks: alpha -> beta" in text
    # remove_relation
    memory.state.apply_ops(
        session_id="s1",
        operations=[
            {"op": "remove_relation", "relation": "blocks", "from": "alpha", "to": "beta", "evidence_quote": "alpha blocks beta"}
        ],
        source_turn_id="t2",
        evidence_text=evidence,
    )
    assert memory.state.get("s1")["relations"]["blocks"] == []


def test_layer_budget_truncation_is_marked(tmp_path: Path) -> None:
    memory = Memory.local(path=tmp_path / "mem")
    memory.layer_budgets = {"curated": 20}
    memory.curated.apply(
        action="add", content="x" * 100, scope="user", session_id="s1"
    )
    text, summary = memory.build_context(session_id="s1", query="anything")
    assert "[ariadne: layer curated truncated to budget 20 chars]" in text
