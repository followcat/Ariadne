from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .json_file import locked_read_json, locked_update_json, locked_write_json
from .state import ConversationStateStore


ProjectorFn = Callable[[str, str], Awaitable[list[dict[str, Any]]]]


@dataclass
class ProjectionJob:
    job_id: str
    session_id: str
    turn_id: str
    evidence_text: str
    status: str  # pending|leased|succeeded|failed|no_change
    attempts: int = 0
    lease_owner: str = ""
    lease_until: float = 0.0
    error: str = ""


class ProjectionWorker:
    """Background/fenced projection queue for conversation state.

    Personal mode can run jobs inline via drain(), or lease/process like a worker.
    Job file is fcntl-locked for safe agent + sub-process concurrency.
    """

    def __init__(
        self,
        path: Path,
        state_store: ConversationStateStore,
        *,
        lease_seconds: float = 30.0,
        max_attempts: int = 3,
    ) -> None:
        self.path = Path(path)
        self.state_store = state_store
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"jobs": []})

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default={"jobs": []})
        if not isinstance(data, dict):
            return {"jobs": []}
        data.setdefault("jobs", [])
        return data

    def _write(self, data: dict[str, Any]) -> None:
        locked_write_json(self.path, data)

    def enqueue(self, *, session_id: str, turn_id: str, evidence_text: str) -> str:
        job_id = uuid.uuid4().hex[:12]

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            data.setdefault("jobs", []).append(
                {
                    "job_id": job_id,
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "evidence_text": evidence_text,
                    "status": "pending",
                    "attempts": 0,
                    "lease_owner": "",
                    "lease_until": 0.0,
                    "error": "",
                }
            )
            return data

        locked_update_json(self.path, mut, default={"jobs": []})
        return job_id

    def claim(self, *, worker_id: str, session_id: str | None = None) -> dict[str, Any] | None:
        """Claim next job in per-session enqueue order (turn pipeline order).

        Within a session, only the earliest unfinished job is claimable — later
        turns wait until earlier ones succeed/fail/no_change.
        Atomic under exclusive lock (safe across processes).
        """
        now = time.time()
        claimed: dict[str, Any] | None = None

        def unfinished(job: dict[str, Any]) -> bool:
            st = job.get("status")
            return st in {"pending", "leased"}

        def claimable(job: dict[str, Any]) -> bool:
            st = job.get("status")
            if st == "pending":
                return True
            if st == "leased" and float(job.get("lease_until") or 0) <= now:
                return True
            return False

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            jobs: list[dict[str, Any]] = list(data.get("jobs") or [])
            first_unfinished_by_session: dict[str, dict[str, Any]] = {}
            for job in jobs:
                sid = str(job.get("session_id") or "")
                if session_id is not None and sid != session_id:
                    continue
                if not unfinished(job):
                    continue
                if sid not in first_unfinished_by_session:
                    first_unfinished_by_session[sid] = job

            for job in first_unfinished_by_session.values():
                if not claimable(job):
                    continue
                job["status"] = "leased"
                job["lease_owner"] = worker_id
                job["lease_until"] = now + self.lease_seconds
                job["attempts"] = int(job.get("attempts") or 0) + 1
                claimed = dict(job)
                break
            data["jobs"] = jobs
            return data

        locked_update_json(self.path, mut, default={"jobs": []})
        return claimed

    def pending_lag(self, session_id: str) -> int:
        """Number of unfinished jobs (pending/active lease) for a session."""
        now = time.time()
        n = 0
        for job in self.list_jobs(session_id=session_id):
            st = job.get("status")
            if st == "pending":
                n += 1
            elif st == "leased" and float(job.get("lease_until") or 0) > now:
                n += 1
        return n

    def complete(self, job_id: str, *, status: str, error: str = "") -> None:
        def mut(data: dict[str, Any]) -> dict[str, Any]:
            for job in data.get("jobs") or []:
                if job.get("job_id") == job_id:
                    job["status"] = status
                    job["error"] = error
                    job["lease_owner"] = ""
                    job["lease_until"] = 0.0
                    break
            return data

        locked_update_json(self.path, mut, default={"jobs": []})

    def list_jobs(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        data = self._read()
        jobs = data.get("jobs") or []
        if session_id:
            jobs = [j for j in jobs if j.get("session_id") == session_id]
        return list(jobs)

    async def process_one(self, projector: ProjectorFn, *, worker_id: str = "local") -> dict[str, Any] | None:
        job = self.claim(worker_id=worker_id)
        if job is None:
            return None
        try:
            ops = await projector(job["evidence_text"], job["turn_id"])
            if not ops:
                self.complete(job["job_id"], status="no_change")
                return {"job_id": job["job_id"], "status": "no_change"}
            result = self.state_store.apply_ops(
                session_id=job["session_id"],
                operations=ops,
                source_turn_id=job["turn_id"],
                evidence_text=job["evidence_text"],
            )
            self.complete(job["job_id"], status="succeeded")
            return {"job_id": job["job_id"], "status": "succeeded", "result": result}
        except Exception as exc:  # noqa: BLE001
            status = "failed" if int(job.get("attempts") or 0) >= self.max_attempts else "pending"
            self.complete(job["job_id"], status=status, error=f"{type(exc).__name__}: {exc}")
            return {"job_id": job["job_id"], "status": status, "error": str(exc)}

    async def drain(self, projector: ProjectorFn, *, max_jobs: int = 20) -> list[dict[str, Any]]:
        results = []
        for _ in range(max_jobs):
            item = await self.process_one(projector)
            if item is None:
                break
            results.append(item)
        return results
