from pathlib import Path

from ariadne.memory import Memory
from ariadne.memory.user_model import UserModelStore


def test_low_info_skips_retrieved_and_keeps_pinned(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    assert mem.user_model is not None
    mem.user_model.upsert(
        entry_type="preference",
        key="tables",
        value="prefer tables over prose",
        source="user_explicit",
        confidence=1.0,
        scope="user",
    )
    mem.user_model.upsert(
        entry_type="relation",
        key="coworker",
        value="speaks rust",
        source="user_explicit",
        confidence=0.8,
        scope="user",
    )
    mem.apply_curated(
        action="add",
        content="the cobalt migration used postgres",
        scope="workspace",
        session_id="s1",
    )
    text, summary = mem.build_context(session_id="s1", query="好的")
    names = {layer.name: layer for layer in summary.layers}
    assert names["retrieved_profile"].status == "skipped"
    assert names["semantic"].status == "skipped"
    assert "prefer tables over prose" in text
    assert "cobalt migration" not in text
    assert "speaks rust" not in text


def test_informative_query_selects_profile(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    mem.apply_curated(
        action="add",
        content="the cobalt migration used postgres",
        scope="workspace",
        session_id="s1",
    )
    mem.apply_curated(
        action="add",
        content="favorite snack is almonds",
        scope="workspace",
        session_id="s1",
    )
    text, summary = mem.build_context(session_id="s1", query="what happened in the cobalt migration")
    assert "cobalt migration" in text
    assert "almonds" not in text
    names = {layer.name: layer.status for layer in summary.layers}
    assert names["retrieved_profile"] == "used"


def test_immediate_deixis_skips_semantic(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    mem.index_turn(
        session_id="s1",
        turn_id="t1",
        user_text="we discussed the cobalt migration",
        assistant_text="noted",
    )
    _text, summary = mem.build_context(session_id="s1", query="刚才那句什么意思")
    names = {layer.name: layer.status for layer in summary.layers}
    assert names["semantic"] == "skipped"
    assert names["turn_summary"] == "skipped"


def test_low_info_keeps_working_set_on_large_state(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    for index in range(80):
        mem.state.apply_ops(
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
    text, summary = mem.build_context(session_id="s1", query="好的")
    assert "CONVERSATION_STATE_WORKING_SET" in text
    names = {layer.name: layer for layer in summary.layers}
    assert names["conversation_state"].status == "used"
    assert names["conversation_state"].omitted_count >= 0
    assert names["retrieved_profile"].status == "skipped"


def test_typed_and_curated_duplicate_appears_once(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    assert mem.user_model is not None
    mem.user_model.upsert(
        entry_type="preference",
        key="tables",
        value="prefer tables over prose",
        source="user_explicit",
        confidence=1.0,
        scope="user",
    )
    mem.apply_curated(
        action="add",
        content="prefer tables over prose",
        scope="user",
        session_id="s1",
    )
    text, _summary = mem.build_context(session_id="s1", query="formatting")
    assert text.count("prefer tables over prose") == 1


def test_user_model_store_still_renders(tmp_path: Path) -> None:
    store = UserModelStore(tmp_path / "user_model.json")
    store.upsert(
        entry_type="preference",
        key="tone",
        value="brief",
        source="user_explicit",
        confidence=1.0,
        scope="user",
    )
    text, count = store.render()
    assert count == 1
    assert "brief" in text
