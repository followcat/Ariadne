"""Cross-process JSON locks + LLM compressor under a running event loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path
from typing import Any

from ariadne.memory.json_file import locked_read_json, locked_update_json
from ariadne.memory.llm_summary import make_llm_compressor, _run_coro_sync
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.model.fake import FakeModel


def test_locked_update_no_lost_writes(tmp_path: Path) -> None:
    path = tmp_path / "shared.json"
    locked_update_json(path, lambda d: {"n": 0}, default={"n": 0})
    errors: list[BaseException] = []

    def bump(_: int) -> None:
        try:
            for _ in range(40):
                def mut(data: dict[str, Any]) -> dict[str, Any]:
                    data["n"] = int(data.get("n") or 0) + 1
                    return data

                locked_update_json(path, mut, default={"n": 0})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bump, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    final = locked_read_json(path, default={})
    assert final["n"] == 160  # 4 * 40


def test_summary_process_pending_concurrent_with_enqueue(tmp_path: Path) -> None:
    store = TurnSummaryStore(tmp_path / "sum.json")
    for i in range(20):
        store.enqueue(
            session_id="s1",
            turn_id=f"t{i}",
            source_text=f"User said fact-{i} and path NOTES.md. " * 3,
        )

    def worker() -> None:
        store.process_pending(session_id="s1", max_jobs=50)

    def enqueuer() -> None:
        for i in range(20, 30):
            store.enqueue(
                session_id="s1",
                turn_id=f"t{i}",
                source_text=f"extra turn {i} with VALUE-{i}",
            )

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=enqueuer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    store.process_pending(session_id="s1", max_jobs=50)
    # All 30 turns accounted for (ready or pending→then ready)
    data = store._read()
    assert len(data.get("s1") or {}) == 30
    ready = store.list_ready("s1", limit=50)
    assert len(ready) == 30


def test_projection_claim_atomic_across_threads(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    for i in range(10):
        worker.enqueue(session_id="s1", turn_id=f"t{i}", evidence_text=f"e{i}")

    claimed: list[str] = []
    lock = threading.Lock()

    def claimer(wid: str) -> None:
        while True:
            job = worker.claim(worker_id=wid)
            if job is None:
                return
            with lock:
                claimed.append(job["job_id"])
            worker.complete(job["job_id"], status="succeeded")

    threads = [threading.Thread(target=claimer, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 10
    assert len(set(claimed)) == 10  # no double-claim


def test_run_coro_sync_inside_running_loop() -> None:
    async def inner() -> str:
        return "from-thread"

    async def outer() -> str:
        # Nested: outer has running loop; _run_coro_sync must still work.
        return _run_coro_sync(inner())

    assert asyncio.run(outer()) == "from-thread"


def test_llm_compressor_called_under_running_loop() -> None:
    calls: list[list[dict[str, Any]]] = []

    def script(messages: list[dict[str, Any]], tools: Any) -> dict[str, Any]:
        calls.append(list(messages))
        return {"content": "LLM summary: NOTES.md preferred short bullets."}

    model = FakeModel(script=script)
    compress = make_llm_compressor(model, max_chars=120, fallback=True)
    assert getattr(compress, "kind") == "llm"

    src = (
        "User asked for notes. Prefer short bullets. Path is NOTES.md. "
        "Lots of filler text to force compression path beyond half budget. " * 8
    )

    async def simulate_turn_end() -> str:
        # Matches real turn path: process_pending → compress while loop is running.
        return compress(src)

    out = asyncio.run(simulate_turn_end())
    assert calls, "LLM model.complete must be invoked under a running event loop"
    assert "NOTES" in out or "short bullets" in out or "LLM summary" in out
    assert len(out) <= 120


def test_summary_store_records_llm_compressor_kind(tmp_path: Path) -> None:
    calls = {"n": 0}

    def script(messages, tools):
        calls["n"] += 1
        return {"content": "Route is SOUTH-29 for harbor."}

    model = FakeModel(script=script)
    store = TurnSummaryStore(
        tmp_path / "sum.json",
        compressor=make_llm_compressor(model, max_chars=100, fallback=True),
    )
    long = "Set route SOUTH-29 for glass harbor. " + ("detail " * 50)

    async def turn_like() -> int:
        store.enqueue(session_id="s1", turn_id="t1", source_text=long)
        return store.process_pending(session_id="s1")

    n = asyncio.run(turn_like())
    assert n == 1
    assert calls["n"] == 1
    data = store._read()
    payload = data["s1"]["t1"]
    assert payload["status"] == "ready"
    assert payload["compressor"] == "llm"
    assert "SOUTH-29" in payload["summary_text"]


def test_state_apply_ops_locked_rmw(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            store.apply_ops(
                session_id="s1",
                source_turn_id=f"t{i}",
                evidence_text=f"entity E{i} value V{i}",
                operations=[
                    {
                        "op": "ensure_entity",
                        "entity_id": f"e:{i}",
                        "type": "item",
                        "evidence_quote": f"E{i}",
                    },
                    {
                        "op": "set_attribute",
                        "entity_id": f"e:{i}",
                        "key": "v",
                        "value": f"V{i}",
                        "evidence_quote": f"V{i}",
                    },
                ],
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    st = store.get("s1")
    assert len(st["entities"]) == 8
