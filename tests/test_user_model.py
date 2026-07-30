from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory import Memory
from ariadne.memory.user_model import UserModelStore


def test_typed_user_model_scopes_history_and_cas(tmp_path: Path) -> None:
    store = UserModelStore(tmp_path / "user-model.json")
    created = store.upsert(
        entry_type="preference",
        key="answer_style",
        value="concise",
        source="user_explicit",
        confidence=1.0,
        scope="user",
    )
    workspace = store.upsert(
        entry_type="constraint",
        key="runtime",
        value="python3.13",
        source="user_explicit",
        confidence=1.0,
        scope="workspace",
        workspace_key="/project/a",
    )
    assert len(store.list(workspace_key="/project/a")) == 2
    assert [row["entry_id"] for row in store.list(workspace_key="/project/b")] == [
        created["entry_id"]
    ]

    updated = store.upsert(
        entry_id=created["entry_id"],
        expected_revision=1,
        entry_type="preference",
        key="answer_style",
        value="detailed",
        source="user_explicit",
        confidence=1.0,
        scope="user",
    )
    assert updated["revision"] == 2
    assert updated["history"][0]["value"] == "concise"
    assert updated["history"][0]["status"] == "superseded"
    with pytest.raises(AriadneError) as caught:
        store.expire(entry_id=created["entry_id"], expected_revision=1)
    assert caught.value.error.code == "ARIADNE_USER_MODEL_CONFLICT"

    expired = store.expire(entry_id=workspace["entry_id"], expected_revision=1)
    assert expired["status"] == "expired"
    assert workspace["entry_id"] not in {
        row["entry_id"] for row in store.list(workspace_key="/project/a")
    }


def test_user_model_enters_layered_memory_context(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    memory.workspace_key = "/project/a"
    assert memory.user_model is not None
    memory.user_model.upsert(
        entry_type="capability",
        key="python",
        value="advanced",
        source="user_explicit",
        confidence=0.9,
        scope="user",
    )
    text, summary = memory.build_context(session_id="s1", query="help")
    assert "capability:python='advanced'" in text
    layer = next(item for item in summary.layers if item.name == "user_model")
    assert layer.status == "used"
