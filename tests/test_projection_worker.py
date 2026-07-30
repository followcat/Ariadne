import asyncio
import json
import time
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.projection import (
    ProjectionDecision,
    ProjectionWorker,
    make_llm_projector,
)
from ariadne.memory.state import ConversationStateStore
from ariadne.model.base import ModelExchange
from ariadne.types import Message, Usage


def test_projection_lease_and_complete(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    job_id = worker.enqueue(session_id="s1", turn_id="t1", evidence_text="Created file FOO.md")

    async def projector(evidence: str, turn_id: str):
        return ProjectionDecision(
            decision="apply",
            reason="the file is explicit in evidence",
            operations=[
                {
                    "op": "ensure_entity",
                    "entity_id": "doc:foo",
                    "type": "file",
                    "evidence_quote": "FOO.md",
                }
            ],
        )

    async def run():
        result = await worker.process_one(projector, worker_id="w1")
        return result

    result = asyncio.run(run())
    assert result is not None
    assert result["status"] == "succeeded"
    jobs = worker.list_jobs(session_id="s1")
    assert jobs[0]["job_id"] == job_id
    assert jobs[0]["status"] == "succeeded"
    text, n = state.render("s1")
    assert n == 1
    assert "doc:foo" in text or "FOO" in text


def test_projection_rejects_stale_lease_completion(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(
        path=tmp_path / "jobs.json",
        state_store=state,
        lease_seconds=10,
    )
    job_id = worker.enqueue(session_id="s1", turn_id="t1", evidence_text="evidence")
    stale = worker.claim(worker_id="worker-old")
    assert stale is not None
    jobs = worker._read()
    jobs["jobs"][0]["lease_until"] = time.time() - 1
    worker._write(jobs)
    current = worker.claim(worker_id="worker-new")
    assert current is not None
    assert current["lease_token"] > stale["lease_token"]

    with pytest.raises(AriadneError) as caught:
        worker.complete(
            job_id,
            worker_id="worker-old",
            lease_token=stale["lease_token"],
            status="succeeded",
        )
    assert caught.value.error.code == "ARIADNE_MEMORY_PROJECTION_LEASE_LOST"

    worker.complete(
        job_id,
        worker_id="worker-new",
        lease_token=current["lease_token"],
        status="succeeded",
    )
    assert worker.list_jobs()[0]["status"] == "succeeded"


def test_projection_state_apply_is_idempotent_by_job_key(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    kwargs = {
        "session_id": "s1",
        "operations": [
            {
                "op": "ensure_entity",
                "entity_id": "doc:one",
                "type": "file",
                "evidence_quote": "one.txt",
            }
        ],
        "source_turn_id": "t1",
        "evidence_text": "created one.txt",
        "expected_parent_version": 0,
        "idempotency_key": "projection:job-1",
    }
    first = state.apply_ops(**kwargs)
    replay = state.apply_ops(**kwargs)
    assert first["version"] == 1
    assert replay["version"] == 1
    assert replay["idempotent_replay"] is True
    assert state.version("s1") == 1


def test_projection_requires_explicit_no_change_decision(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    worker.enqueue(session_id="s1", turn_id="t1", evidence_text="nothing durable")

    async def projector(evidence: str, turn_id: str) -> ProjectionDecision:
        return ProjectionDecision(
            decision="confirmed_no_change",
            operations=[],
            reason="the evidence contains no state change",
        )

    result = asyncio.run(worker.process_one(projector))
    assert result == {
        "job_id": result["job_id"],
        "status": "confirmed_no_change",
        "reason": "the evidence contains no state change",
    }
    job = worker.list_jobs(session_id="s1")[0]
    assert job["status"] == "confirmed_no_change"
    assert job["reason"] == "the evidence contains no state change"


def test_projection_protocol_errors_are_terminal(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    worker.enqueue(session_id="s1", turn_id="t1", evidence_text="nothing")

    async def invalid_projector(evidence: str, turn_id: str):
        return []

    result = asyncio.run(worker.process_one(invalid_projector))
    assert result is not None
    assert result["status"] == "failed"
    job = worker.list_jobs(session_id="s1")[0]
    assert job["status"] == "failed"
    assert "ProjectionDecision" in job["error"]


def test_llm_projector_requires_one_schema_valid_tool_call() -> None:
    class Model:
        model = "fake"

        async def complete(self, **kwargs):
            assert kwargs["tool_choice"]["function"]["name"] == "project_conversation_state"
            return ModelExchange(
                message=Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "p1",
                            "type": "function",
                            "function": {
                                "name": "project_conversation_state",
                                "arguments": json.dumps(
                                    {
                                        "decision": "confirmed_no_change",
                                        "operations": [],
                                        "reason": "no explicit state",
                                    }
                                ),
                            },
                        }
                    ],
                ),
                usage=Usage(),
                raw={},
            )

    decision = asyncio.run(make_llm_projector(Model())("hello", "t1"))
    assert decision.decision == "confirmed_no_change"


def test_llm_projector_rejects_free_text() -> None:
    class Model:
        model = "fake"

        async def complete(self, **kwargs):
            return ModelExchange(
                message=Message(role="assistant", content="nothing to remember"),
                usage=Usage(),
                raw={},
            )

    with pytest.raises(AriadneError) as caught:
        asyncio.run(make_llm_projector(Model())("hello", "t1"))
    assert caught.value.error.code == "ARIADNE_MEMORY_PROJECTOR_PROTOCOL"
