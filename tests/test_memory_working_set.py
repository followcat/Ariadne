from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory import Memory
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.state_sqlite import resolve_db_path


def _apply_route(store: ConversationStateStore, *, session: str, turn: str, value: str) -> None:
    quote = f"route is {value}"
    store.apply_ops(
        session_id=session,
        source_turn_id=turn,
        evidence_text=quote,
        operations=[
            {"op": "ensure_entity", "entity_id": "route", "evidence_quote": quote},
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": value,
                "evidence_quote": quote,
            },
        ],
    )


def test_small_state_is_complete_without_query(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _apply_route(store, session="s1", turn="t1", value="SOUTH")
    working = store.assemble_working_set(
        "s1", "", soft_chars=6000, hard_chars=8000
    )
    assert working.selection_mode == "complete"
    assert "SOUTH" in working.text
    assert "members=[]" not in working.text


def test_large_state_selects_and_lookup_recovers(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    for index in range(80):
        quote = f"entity e{index} city={index}"
        store.apply_ops(
            session_id="s1",
            source_turn_id=f"t{index}",
            evidence_text=quote,
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": f"e{index}",
                    "evidence_quote": f"e{index}",
                },
                {
                    "op": "set_attribute",
                    "entity_id": f"e{index}",
                    "key": "city",
                    "value": f"city-{index}",
                    "evidence_quote": f"city={index}",
                },
            ],
        )
    working = store.assemble_working_set(
        "s1", "what is city for e77", soft_chars=6000, hard_chars=8000
    )
    assert working.selection_mode == "selected"
    assert working.omitted_count > 0
    assert working.char_count <= 8000
    assert "e77" in working.text
    page = store.lookup(session_id="s1", query="e3", limit=8)
    assert any(item["ref"] == "e3" or "e3" in str(item.get("ref")) for item in page["items"])
    assert page["semantic_status"] == "disabled"


def test_selected_mode_matches_chinese_query(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    for index in range(80):
        store.apply_ops(
            session_id="s1",
            source_turn_id=f"t{index}",
            evidence_text=f"entity e{index} city={index}",
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": f"e{index}",
                    "evidence_quote": f"e{index}",
                },
                {
                    "op": "set_attribute",
                    "entity_id": f"e{index}",
                    "key": "city",
                    "value": f"city-{index}",
                    "evidence_quote": f"city={index}",
                },
            ],
        )
    store.apply_ops(
        session_id="s1",
        source_turn_id="route",
        evidence_text="route is SOUTH 路线",
        operations=[
            {"op": "ensure_entity", "entity_id": "route", "evidence_quote": "route is SOUTH"},
            {"op": "set_alias", "entity_id": "route", "alias": "路线", "evidence_quote": "路线"},
            {
                "op": "set_attribute",
                "entity_id": "route",
                "key": "direction",
                "value": "SOUTH",
                "evidence_quote": "route is SOUTH",
            },
        ],
    )
    city = store.assemble_working_set(
        "s1", "e77的城市", soft_chars=6000, hard_chars=8000
    )
    assert city.selection_mode == "selected"
    assert "e77" in city.text
    route = store.assemble_working_set(
        "s1", "现在路线是哪边", soft_chars=6000, hard_chars=8000
    )
    assert route.selection_mode == "selected"
    assert "SOUTH" in route.text
    assert "路线" in route.text


def test_stale_value_does_not_resurface(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _apply_route(store, session="s1", turn="t1", value="NORTH")
    _apply_route(store, session="s1", turn="t2", value="SOUTH")
    working = store.assemble_working_set(
        "s1", "route direction", soft_chars=6000, hard_chars=8000
    )
    assert "SOUTH" in working.text
    assert "NORTH" not in working.text
    page = store.lookup(session_id="s1", query="route.direction")
    values = [str(item.get("payload", {}).get("value")) for item in page["items"]]
    assert "SOUTH" in values
    assert "NORTH" not in values


def test_collection_members_are_not_dumped(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    ops = [
        {"op": "ensure_collection", "name": "todos", "evidence_quote": "todos"},
    ]
    for index in range(12):
        ops.append(
            {
                "op": "collection_append",
                "name": "todos",
                "member": f"task-{index}",
                "evidence_quote": "todos",
            }
        )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="todos",
        operations=ops,
    )
    working = store.assemble_working_set(
        "s1", "todos", soft_chars=6000, hard_chars=8000
    )
    assert "member_count=12" in working.text
    assert "members=[]" not in working.text
    page = store.lookup(session_id="s1", query="todos", limit=5)
    assert page["has_more"] is True
    keys = {item["member"] for item in page["items"]}
    assert len(keys) == 5
    page2 = store.lookup(
        session_id="s1", query="todos", limit=5, cursor=page["next_cursor"]
    )
    keys2 = {item["member"] for item in page2["items"]}
    assert keys.isdisjoint(keys2)
    _apply_route(store, session="s1", turn="t2", value="SOUTH")
    with pytest.raises(AriadneError) as exc_info:
        store.lookup(
            session_id="s1", query="todos", limit=5, cursor=page["next_cursor"]
        )
    assert exc_info.value.error.code == "ARIADNE_MEMORY_STATE_CURSOR_STALE"


def test_json_migrates_once(tmp_path: Path) -> None:
    json_path = tmp_path / "state.json"
    json_path.write_text(
        '{"documents":{"s1":{"state":{"schema_version":1,"entities":{"route":{"type":"generic","aliases":[],"attributes":{"direction":{"value":"SOUTH","status":"active"}},"status":"active"}},"relations":{},"collections":{},"task_goal_bindings":{}},"version":1,"watermark_turn_id":"t1"}},"versions":{}}',
        encoding="utf-8",
    )
    store = ConversationStateStore(json_path)
    assert store.get("s1")["entities"]["route"]["attributes"]["direction"]["value"] == "SOUTH"
    assert json_path.with_name("state.json.migrated").exists()
    assert not json_path.exists()
    assert resolve_db_path(json_path).exists()
    again = ConversationStateStore(json_path)
    assert again.get("s1")["entities"]["route"]["attributes"]["direction"]["value"] == "SOUTH"


def test_dirty_json_and_sqlite_fastfail(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    _apply_route(store, session="s1", turn="t1", value="SOUTH")
    (tmp_path / "state.json").write_text(
        '{"documents":{"s2":{"state":{"schema_version":1,"entities":{},"relations":{},"collections":{}},"version":1}}}',
        encoding="utf-8",
    )
    with pytest.raises(AriadneError) as exc_info:
        ConversationStateStore(tmp_path / "state.json")
    assert exc_info.value.error.code == "ARIADNE_MEMORY_NOT_READY"


def test_lookup_prefers_collection_over_same_ref_entity(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="todos",
        operations=[
            {"op": "ensure_entity", "entity_id": "todos", "evidence_quote": "todos"},
            {"op": "ensure_collection", "name": "todos", "evidence_quote": "todos"},
            {
                "op": "collection_append",
                "name": "todos",
                "member": "task-a",
                "evidence_quote": "todos",
            },
        ],
    )
    page = store.lookup(session_id="s1", query="todos", limit=8)
    assert page["items"]
    assert page["items"][0]["kind"] == "collection_member"
    assert page["items"][0]["member"] == "task-a"


def test_lookup_shrinks_page_instead_of_failing(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    ops = [{"op": "ensure_collection", "name": "bag", "evidence_quote": "bag"}]
    for index in range(8):
        ops.append(
            {
                "op": "collection_append",
                "name": "bag",
                "member": f"item-{'x' * 2500}-{index}",
                "evidence_quote": "bag",
            }
        )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="bag",
        operations=ops,
    )
    page = store.lookup(session_id="s1", query="bag", limit=8)
    assert page["items"]
    assert len(page["items"]) < 8
    assert page["has_more"] is True
    import json

    assert len(json.dumps(page["items"], ensure_ascii=False).encode()) <= 16_000


def test_working_set_never_exceeds_hard_cap(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    for index in range(80):
        store.apply_ops(
            session_id="s1",
            source_turn_id=f"t{index}",
            evidence_text=f"entity e{index}",
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": f"e{index}",
                    "evidence_quote": f"e{index}",
                }
            ],
        )
    working = store.assemble_working_set(
        "s1", "e12", soft_chars=256, hard_chars=256
    )
    assert working.selection_mode == "selected"
    assert working.char_count <= 256


def test_read_and_build_context_share_projection_seq(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    _apply_route(mem.state, session="s1", turn="t1", value="SOUTH")
    _text, summary = mem.build_context(session_id="s1", query="route")
    working = mem.assemble_turn_working_set(session_id="s1", query="route")
    assert summary.projection_seq == working.projection_seq
    assert summary.selection_mode == working.selection_mode
    assert "SOUTH" in working.text


def test_paginated_collection_has_no_gaps_or_dupes(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    ops = [{"op": "ensure_collection", "name": "crowd", "evidence_quote": "crowd"}]
    for index in range(500):
        ops.append(
            {
                "op": "collection_append",
                "name": "crowd",
                "member": f"m{index:04d}",
                "evidence_quote": "crowd",
            }
        )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="crowd",
        operations=ops,
    )
    for index in range(200):
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
    seen: list[str] = []
    cursor = ""
    while True:
        page = store.lookup(session_id="s1", query="crowd", limit=32, cursor=cursor)
        import json

        assert len(json.dumps(page["items"], ensure_ascii=False).encode()) <= 16_000
        seen.extend(str(item["member"]) for item in page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert seen == [f"m{index:04d}" for index in range(500)]


def test_last_good_delta_survives_lag(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    mem.state.apply_ops(
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
    mem.transcript.append({"role": "user", "content": "now SOUTH", "turn_id": "t2"})
    mem.transcript.append({"role": "assistant", "content": "updated", "turn_id": "t2"})
    text, summary = mem.build_context(session_id="s1", query="route?")
    assert "[CONVERSATION_STATE_WORKING_SET]" in text
    assert "[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]" in text
    assert any(layer.status == "stale_delta" for layer in summary.layers)
