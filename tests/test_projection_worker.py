import asyncio
from pathlib import Path

from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.state import ConversationStateStore


def test_projection_lease_and_complete(tmp_path: Path) -> None:
    state = ConversationStateStore(path=tmp_path / "state.json")
    worker = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    job_id = worker.enqueue(session_id="s1", turn_id="t1", evidence_text="Created file FOO.md")

    async def projector(evidence: str, turn_id: str):
        return [
            {
                "op": "ensure_entity",
                "entity_id": "doc:foo",
                "type": "file",
                "evidence_quote": "FOO.md",
            }
        ]

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
