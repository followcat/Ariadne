"""Red regressions for unresolved personal-2C memory correctness gaps.

These tests describe behavior required by ``docs/design/memory-search.md``.
They are intentionally expected to fail until the corresponding production
paths are fixed; keeping them separate makes the remaining gaps explicit.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import ariadne.memory.embeddings as embedding_module
import pytest
from ariadne.memory import Memory
from ariadne.memory.deep_planner import DeepPlan, make_llm_deep_planner
from ariadne.memory.embeddings import HashEmbeddingProvider, OpenAIEmbeddingProvider
from ariadne.memory.semantic import SemanticIndex
from ariadne.model.fake import FakeModel


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_llm_deep_planner_reads_standard_model_exchange() -> None:
    """The host's real ModelPort returns ModelExchange, not a content dict."""

    model = FakeModel(
        script=lambda _messages, _tools: {
            "content": json.dumps(
                {
                    "subqueries": ["alpha migration", "beta rollback"],
                    "alias_extra": ["project-blue"],
                    "rerank_order": ["s1:t1"],
                }
            )
        }
    )
    planner = make_llm_deep_planner(model)

    plan = _run(
        planner.plan(
            query="compare alpha migration and beta rollback",
            aliases=[],
            candidates=[
                {
                    "session_id": "s1",
                    "turn_id": "t1",
                    "snippet": "alpha was migrated before beta",
                }
            ],
        )
    )

    assert plan.notes == "llm_planner"
    assert plan.subqueries == ["alpha migration", "beta rollback"]
    assert plan.alias_extra == ["project-blue"]
    assert plan.rerank_order == ["s1:t1"]


def test_deep_rerank_sees_candidates_added_by_subqueries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deep rerank must run after subquery candidates have been merged."""

    class TwoStagePlanner:
        def __init__(self) -> None:
            self.candidate_calls: list[set[str]] = []

        async def plan(
            self,
            *,
            query: str,
            aliases: list[str],
            candidates: list[dict[str, Any]],
        ) -> DeepPlan:
            _ = query, aliases
            keys = {
                f"{candidate['session_id']}:{candidate['turn_id']}"
                for candidate in candidates
            }
            self.candidate_calls.append(keys)
            if len(self.candidate_calls) == 1:
                return DeepPlan(
                    subqueries=["newly recalled detail"],
                    notes="decompose",
                )
            return DeepPlan(
                rerank_order=["s1:new-turn", "s1:initial-turn"],
                notes="rerank",
            )

    planner = TwoStagePlanner()
    mem = Memory.local(path=tmp_path / "memory", deep_planner=planner)

    async def fake_search_hybrid(**kwargs: Any) -> list[dict[str, Any]]:
        if kwargs["query"] == "newly recalled detail":
            return [
                {
                    "session_id": "s1",
                    "turn_id": "new-turn",
                    "kind": "user",
                    "snippet": "newly recalled detail",
                    "score": 0.1,
                }
            ]
        return [
            {
                "session_id": "s1",
                "turn_id": "initial-turn",
                "kind": "user",
                "snippet": "initial lexical candidate",
                "score": 0.9,
            }
        ]

    monkeypatch.setattr(mem.semantic, "search_hybrid", fake_search_hybrid)
    result = _run(
        mem.memory_search(
            query="find the related decision",
            session_id="s1",
            scope="session",
            mode="deep",
        )
    )

    assert planner.candidate_calls[-1] == {
        "s1:initial-turn",
        "s1:new-turn",
    }
    assert result["hits"][0]["turn_id"] == "new-turn"


def test_user_curated_hit_honors_before_turn_clock(tmp_path: Path) -> None:
    """Every user-scope source must be strictly older than before_turn_id."""

    mem = Memory.local(path=tmp_path / "memory")
    mem.index_turn(
        session_id="s1",
        turn_id="t1",
        user_text="older context",
        assistant_text="",
    )
    mem.index_turn(
        session_id="s1",
        turn_id="t2",
        user_text="later-only durable needle",
        assistant_text="",
    )
    mem.apply_curated(
        action="add",
        content="later-only durable needle",
        scope="user",
        session_id="s1",
        source_turn_id="t2",
    )

    result = _run(
        mem.memory_search(
            query="later-only durable needle",
            session_id="s1",
            scope="user",
            mode="fast",
            before_turn_id="t2",
        )
    )

    assert all(hit["turn_id"] != "t2" for hit in result["hits"])


def test_curated_hit_is_not_reported_as_nonexistent_summary(tmp_path: Path) -> None:
    """L3 curated text must not claim to be an L1 summary that was never made."""

    mem = Memory.local(path=tmp_path / "memory")
    mem.transcript.append(
        {
            "role": "user",
            "content": "origin turn without a summary",
            "turn_id": "t1",
            "session_id": "s1",
        }
    )
    mem.apply_curated(
        action="add",
        content="violet durable preference",
        scope="user",
        session_id="s1",
        source_turn_id="t1",
    )
    assert mem.summaries.render("s1") == ""

    result = _run(
        mem.memory_search(
            query="violet durable preference",
            session_id="s1",
            scope="user",
            mode="fast",
        )
    )
    hit = next(hit for hit in result["hits"] if hit["turn_id"] == "t1")

    assert hit["evidence"]["source"] != "summary"


def test_embedding_writeback_does_not_overwrite_concurrent_turn(tmp_path: Path) -> None:
    """Embedding an old snapshot must preserve turns written while awaiting I/O."""

    class PausingEmbedder:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.started.set()
            await self.release.wait()
            return [[1.0, 0.0] for _ in texts]

    async def scenario() -> set[str]:
        path = tmp_path / "shared-user-episodic.json"
        embedder = PausingEmbedder()
        backfilling = SemanticIndex(
            path=path,
            embedder=embedder,
            embedding_model_id="test:pausing",
        )
        backfilling.index_turn(
            session_id="old-session",
            turn_id="old-turn",
            user_text="old memory awaiting an embedding",
            assistant_text="",
        )

        task = asyncio.create_task(backfilling.ensure_embeddings())
        await asyncio.wait_for(embedder.started.wait(), timeout=1.0)

        concurrent_writer = SemanticIndex(
            path=path,
            embedder=HashEmbeddingProvider(),
            embedding_model_id="hash:64",
        )
        concurrent_writer.index_turn(
            session_id="new-session",
            turn_id="new-turn",
            user_text="new memory written during embedding I/O",
            assistant_text="",
        )

        embedder.release.set()
        await asyncio.wait_for(task, timeout=1.0)
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(chunk["turn_id"]) for chunk in data["chunks"]}

    assert _run(scenario()) == {"old-turn", "new-turn"}


def test_empty_semantic_index_does_not_call_embedding_provider(tmp_path: Path) -> None:
    """An empty corpus has no candidates and needs no remote query embedding."""

    class RecordingEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

    embedder = RecordingEmbedder()
    index = SemanticIndex(
        path=tmp_path / "empty.json",
        embedder=embedder,
        embedding_model_id="test:recording",
    )

    hits = _run(index.search_hybrid(session_id="s1", query="nothing indexed"))

    assert hits == []
    assert embedder.calls == []


def test_openai_embedding_http_does_not_run_on_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The async provider must not execute blocking urllib on the loop thread."""

    urlopen_thread_ids: list[int] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            ).encode("utf-8")

    def fake_urlopen(_request: object, *, timeout: float) -> FakeResponse:
        assert timeout > 0
        urlopen_thread_ids.append(threading.get_ident())
        return FakeResponse()

    monkeypatch.setattr(embedding_module.urllib.request, "urlopen", fake_urlopen)
    provider = OpenAIEmbeddingProvider(
        base_url="https://embedding.example.invalid/v1",
        api_key="test-key",
    )

    async def scenario() -> tuple[int, list[list[float]]]:
        loop_thread_id = threading.get_ident()
        vectors = await provider.embed(["needle"])
        return loop_thread_id, vectors

    loop_thread_id, vectors = _run(scenario())

    assert vectors == [[1.0, 0.0]]
    assert all(thread_id != loop_thread_id for thread_id in urlopen_thread_ids)
