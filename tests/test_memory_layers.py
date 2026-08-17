from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.memory.transcript import TranscriptStore


def _facade(tmp_path: Path) -> MemoryFacade:
    return MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=ConversationStateStore(tmp_path / "s.json"),
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
    )


def test_curated_and_state_and_context(tmp_path: Path) -> None:
    mem = _facade(tmp_path)
    mem.curated.apply(action="add", content="Prefer tables over prose", scope="user", session_id="s1")
    mem.state.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text="route is SOUTH-29 for glass harbor",
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "record:R17",
                "type": "record",
                "evidence_quote": "glass harbor",
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
    mem.transcript.append({"role": "user", "content": "hello", "turn_id": "t1"})
    mem.transcript.append({"role": "assistant", "content": "hi", "turn_id": "t1"})
    mem.summaries.put(session_id="s1", turn_id="t1", summary_text="greeted user")
    mem.semantic.index_turn(
        session_id="s1",
        turn_id="t1",
        user_text="hello",
        assistant_text="hi about glass harbor route",
    )
    text, summary = mem.build_context(session_id="s1", query="what is the route for glass harbor")
    assert "CONVERSATION_STATE_WORKING_SET" in text
    assert "SOUTH-29" in text
    assert summary.state_entity_count >= 1
    names = {layer.name: layer.status for layer in summary.layers}
    assert names["conversation_state"] == "used"
    assert names["retrieved_profile"] == "skipped"


def test_state_requires_evidence(tmp_path: Path) -> None:
    mem = _facade(tmp_path)
    with pytest.raises(AriadneError) as ei:
        mem.state.apply_ops(
            session_id="s1",
            source_turn_id="t1",
            evidence_text="nothing relevant",
            operations=[
                {
                    "op": "set_attribute",
                    "entity_id": "x",
                    "key": "a",
                    "value": 1,
                    "evidence_quote": "missing-quote",
                }
            ],
        )
    assert ei.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"


def test_curated_capacity(tmp_path: Path) -> None:
    store = CuratedStore(tmp_path / "c.json", entry_limit=2)
    store.apply(action="add", content="a", session_id="s")
    store.apply(action="add", content="b", session_id="s")
    with pytest.raises(AriadneError):
        store.apply(action="add", content="c", session_id="s")
