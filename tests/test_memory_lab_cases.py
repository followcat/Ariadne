"""Lightweight memory lab cases ported from design scenarios."""

from __future__ import annotations

import asyncio
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


def test_state_overrides_stale_semantic(tmp_path: Path) -> None:
    mem = _facade(tmp_path)
    mem.semantic.index_turn(
        session_id="s",
        turn_id="old",
        user_text="path is /tmp/old.txt",
        assistant_text="noted old path",
        tool_text="",
        summary_text="old path /tmp/old.txt",
        entity_ids=["file:cfg"],
    )
    evidence = "path updated to /tmp/new.txt for config"
    mem.state.apply_ops(
        session_id="s",
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "file:cfg",
                "type": "file",
                "evidence_quote": "/tmp/new.txt",
            },
            {
                "op": "set_attribute",
                "entity_id": "file:cfg",
                "key": "path",
                "value": "/tmp/new.txt",
                "evidence_quote": "/tmp/new.txt",
            },
        ],
        source_turn_id="new",
        evidence_text=evidence,
    )

    async def run():
        text, report = await mem.build_context_async(session_id="s", query="what is the path")
        return text, report

    text, report = asyncio.run(run())
    assert "/tmp/new.txt" in text
    names = {layer.name: layer.status for layer in report.layers}
    assert names.get("conversation_state") == "used"


def test_curated_cap_fastfail(tmp_path: Path) -> None:
    store = CuratedStore(path=tmp_path / "c.json", entry_limit=2)
    store.apply(action="add", content="a", scope="user", session_id="s")
    store.apply(action="add", content="b", scope="user", session_id="s")
    with pytest.raises(AriadneError):
        store.apply(action="add", content="c", scope="user", session_id="s")
