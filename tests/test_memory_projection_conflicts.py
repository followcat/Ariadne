from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.state import ConversationStateStore


def _set(
    store: ConversationStateStore,
    *,
    turn: str,
    value: str,
    authority: str,
    memory_type: str = "fact",
) -> dict:
    quote = f"route is {value}"
    return store.apply_ops(
        session_id="s1",
        source_turn_id=turn,
        evidence_text=quote,
        operations=[
            {
                "op": "set_attribute",
                "entity_id": "project",
                "key": "route",
                "value": value,
                "authority": authority,
                "memory_type": memory_type,
                "evidence_quote": quote,
            }
        ],
    )


def test_lower_authority_cannot_blindly_overwrite_attribute(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _set(store, turn="t1", value="NORTH", authority="user_explicit")

    with pytest.raises(AriadneError) as caught:
        _set(store, turn="t2", value="SOUTH", authority="model_inferred")

    assert caught.value.error.code == "ARIADNE_MEMORY_CONFLICT"
    assert store.version("s1") == 1
    current = store.get("s1")["entities"]["project"]["attributes"]["route"]
    assert current["value"] == "NORTH"
    assert current["status"] == "active"


def test_superseded_history_and_as_of_restore_prior_value(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _set(store, turn="t1", value="NORTH", authority="user_explicit")
    _set(store, turn="t2", value="WEST", authority="user_explicit")

    current = store.get("s1")["entities"]["project"]["attributes"]["route"]
    assert current["value"] == "WEST"
    assert current["history"][0]["value"] == "NORTH"
    assert current["history"][0]["status"] == "superseded"
    assert current["history"][0]["superseded_by"] == current["record_id"]

    old = store.get_as_of("s1", allowed_turn_ids={"t1"})
    restored = old["entities"]["project"]["attributes"]["route"]
    assert restored["value"] == "NORTH"
    assert restored["status"] == "active"


def test_attribute_expiration_is_versioned_and_evidence_bound(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _set(
        store,
        turn="t1",
        value="concise",
        authority="user_explicit",
        memory_type="preference",
    )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t2",
        evidence_text="no longer prefer concise",
        operations=[
            {
                "op": "expire_attribute",
                "entity_id": "project",
                "key": "route",
                "authority": "user_explicit",
                "evidence_quote": "no longer prefer concise",
            }
        ],
    )
    current = store.get("s1")["entities"]["project"]["attributes"]["route"]
    assert current["status"] == "expired"
    assert current["memory_type"] == "preference"
    assert current["history"][-1]["status"] == "superseded"
