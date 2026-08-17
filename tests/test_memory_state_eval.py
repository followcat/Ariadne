"""Deterministic conversation-state evaluation (no LLM)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.state_sqlite import canonical_state_hash


def _store(tmp_path: Path) -> ConversationStateStore:
    return ConversationStateStore(tmp_path / "state.json")


def test_replay_hash_matches_current_document(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="route is SOUTH",
        operations=[
            {"op": "ensure_entity", "entity_id": "route", "evidence_quote": "route is SOUTH"},
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "SOUTH",
                "evidence_quote": "route is SOUTH",
            },
        ],
    )
    state = store.get("s1")
    doc = store._db.get_document("s1")
    assert doc is not None
    assert doc["projection_hash"] == canonical_state_hash(state)
    replayed = store.get_as_of("s1", allowed_turn_ids={"t1"})
    assert replayed["entities"]["route"]["attributes"]["direction"]["value"] == "SOUTH"


def test_complete_then_selected_switch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_ops(
        session_id="s1",
        source_turn_id="t0",
        evidence_text="seed",
        operations=[{"op": "ensure_entity", "entity_id": "seed", "evidence_quote": "seed"}],
    )
    small = store.assemble_working_set("s1", "seed", soft_chars=6000, hard_chars=8000)
    assert small.selection_mode == "complete"
    for index in range(60):
        store.apply_ops(
            session_id="s1",
            source_turn_id=f"t{index + 1}",
            evidence_text=f"entity e{index}",
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": f"e{index}",
                    "evidence_quote": f"e{index}",
                }
            ],
        )
    large = store.assemble_working_set("s1", "e42", soft_chars=6000, hard_chars=8000)
    assert large.selection_mode == "selected"
    assert large.omitted_count > 0
    assert large.char_count <= 8000
    assert "e42" in large.text


def test_stale_cursor_and_stale_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="route is NORTH",
        operations=[
            {"op": "ensure_entity", "entity_id": "route", "evidence_quote": "route is NORTH"},
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "NORTH",
                "evidence_quote": "route is NORTH",
            },
        ],
    )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1b",
        evidence_text="bag",
        operations=[
            {"op": "ensure_collection", "name": "bag", "evidence_quote": "bag"},
            * [
                {
                    "op": "collection_append",
                    "name": "bag",
                    "member": f"m{index}",
                    "evidence_quote": "bag",
                }
                for index in range(6)
            ],
        ],
    )
    first = store.lookup(session_id="s1", query="bag", limit=2)
    assert first["next_cursor"]
    store.apply_ops(
        session_id="s1",
        source_turn_id="t2",
        evidence_text="route is SOUTH",
        operations=[
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "SOUTH",
                "evidence_quote": "route is SOUTH",
            }
        ],
    )
    with pytest.raises(AriadneError) as caught:
        store.lookup(session_id="s1", query="bag", cursor=first["next_cursor"])
    assert caught.value.error.code == "ARIADNE_MEMORY_STATE_CURSOR_STALE"
    working = store.assemble_working_set("s1", "route", soft_chars=6000, hard_chars=8000)
    assert "SOUTH" in working.text
    assert "NORTH" not in working.text


@pytest.mark.skipif(
    os.environ.get("ARIADNE_MEMORY_SOAK") != "1",
    reason="set ARIADNE_MEMORY_SOAK=1 to run personal-scale soak",
)
def test_personal_scale_soak(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ops = [{"op": "ensure_collection", "name": "bag", "evidence_quote": "bag"}]
    for index in range(2000):
        ops.append(
            {
                "op": "collection_append",
                "name": "bag",
                "member": f"m{index:04d}",
                "evidence_quote": "bag",
            }
        )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t0",
        evidence_text="bag",
        operations=ops,
    )
    for index in range(1000):
        store.apply_ops(
            session_id="s1",
            source_turn_id=f"e{index}",
            evidence_text=f"entity n{index}",
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": f"n{index}",
                    "evidence_quote": f"n{index}",
                }
            ],
        )
    working = store.assemble_working_set("s1", "n500", soft_chars=6000, hard_chars=8000)
    assert working.char_count <= 8000
    started = time.perf_counter()
    samples = []
    for _ in range(8):
        t0 = time.perf_counter()
        store.lookup(session_id="s1", query="bag", limit=32)
        samples.append(time.perf_counter() - t0)
    elapsed = time.perf_counter() - started
    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1]
    assert p95 < 0.1, f"lookup p95 {p95:.3f}s over 100ms ({elapsed:.2f}s total)"
