from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..errors import AriadneError, app_error
from ..model.base import ModelPort
from .json_file import locked_read_json, locked_update_json, locked_write_json
from .state import ConversationStateStore


@dataclass(slots=True)
class ProjectionDecision:
    decision: Literal["apply", "confirmed_no_change"]
    operations: list[dict[str, Any]]
    reason: str


ProjectorFn = Callable[[str, str], Awaitable[ProjectionDecision]]

PROJECT_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "project_conversation_state",
        "description": (
            "Project only explicit current-session facts, goals, preferences, and hypotheses "
            "from the supplied evidence. Every operation needs a verbatim evidence_quote."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "operations", "reason"],
            "properties": {
                "decision": {"type": "string", "enum": ["apply", "confirmed_no_change"]},
                "reason": {"type": "string", "minLength": 1},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["op", "evidence_quote"],
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": [
                                    "ensure_entity",
                                    "set_alias",
                                    "set_attribute",
                                    "expire_attribute",
                                    "set_status",
                                    "set_relation",
                                    "remove_relation",
                                    "ensure_collection",
                                    "collection_append",
                                    "collection_remove",
                                    "collection_move",
                                ],
                            },
                            "entity_id": {"type": "string"},
                            "type": {"type": "string"},
                            "alias": {"type": "string"},
                            "key": {"type": "string"},
                            "value": {},
                            "memory_type": {
                                "type": "string",
                                "enum": ["fact", "preference", "goal", "hypothesis"],
                            },
                            "authority": {
                                "type": "string",
                                "enum": ["model_inferred", "tool_observed", "user_explicit"],
                            },
                            "status": {"type": "string"},
                            "relation": {"type": "string"},
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "name": {"type": "string"},
                            "member": {"type": "string"},
                            "to_index": {"type": "integer"},
                            "evidence_quote": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    },
}


def make_llm_projector(model: ModelPort) -> ProjectorFn:
    """Build a strict, evidence-bound projector. No free-text/fallback parse."""

    async def project(evidence: str, turn_id: str) -> ProjectionDecision:
        exchange = await model.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a conservative state projector. Use only the evidence block. "
                        "Do not infer unstated values. Call project_conversation_state exactly once."
                    ),
                },
                {
                    "role": "user",
                    "content": f"turn_id={turn_id}\n[EVIDENCE]\n{evidence}",
                },
            ],
            tools=[PROJECT_STATE_TOOL],
            tool_choice={
                "type": "function",
                "function": {"name": "project_conversation_state"},
            },
            temperature=0.0,
            max_tokens=2500,
        )
        calls = exchange.message.tool_calls or []
        if len(calls) != 1 or str((calls[0].get("function") or {}).get("name") or "") != (
            "project_conversation_state"
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                    "projector must return exactly one project_conversation_state call",
                )
            )
        raw = (calls[0].get("function") or {}).get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            Draft202012Validator(PROJECT_STATE_TOOL["function"]["parameters"]).validate(args)
        except (TypeError, ValueError, ValidationError) as exc:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                    f"projector returned invalid structured output: {exc}",
                )
            ) from exc
        decision = str(args["decision"])
        operations = list(args["operations"])
        if decision == "apply" and not operations:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                    "projector decision=apply requires operations",
                )
            )
        if decision == "confirmed_no_change" and operations:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                    "confirmed_no_change cannot include operations",
                )
            )
        return ProjectionDecision(
            decision=decision,  # type: ignore[arg-type]
            operations=operations,
            reason=str(args["reason"]),
        )

    return project


@dataclass
class ProjectionJob:
    job_id: str
    session_id: str
    turn_id: str
    evidence_text: str
    status: str  # pending|leased|succeeded|failed|confirmed_no_change
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
        turns wait until earlier ones succeed/fail/confirmed_no_change.
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

    def complete(
        self,
        job_id: str,
        *,
        status: str,
        error: str = "",
        reason: str = "",
    ) -> None:
        def mut(data: dict[str, Any]) -> dict[str, Any]:
            for job in data.get("jobs") or []:
                if job.get("job_id") == job_id:
                    job["status"] = status
                    job["error"] = error
                    job["reason"] = reason
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

    async def process_one(
        self,
        projector: ProjectorFn,
        *,
        worker_id: str = "local",
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        job = self.claim(worker_id=worker_id, session_id=session_id)
        if job is None:
            return None
        try:
            decision = await projector(job["evidence_text"], job["turn_id"])
            if not isinstance(decision, ProjectionDecision):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                        "projector must return ProjectionDecision",
                    )
                )
            if decision.decision not in {"apply", "confirmed_no_change"}:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                        f"unknown projector decision: {decision.decision}",
                    )
                )
            if decision.decision == "confirmed_no_change":
                self.complete(
                    job["job_id"],
                    status="confirmed_no_change",
                    reason=decision.reason,
                )
                return {
                    "job_id": job["job_id"],
                    "status": "confirmed_no_change",
                    "reason": decision.reason,
                }
            if not decision.operations:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                        "decision=apply requires at least one operation",
                    )
                )
            parent_version = self.state_store.version(job["session_id"])
            result = self.state_store.apply_ops(
                session_id=job["session_id"],
                operations=decision.operations,
                source_turn_id=job["turn_id"],
                evidence_text=job["evidence_text"],
                expected_parent_version=parent_version,
            )
            self.complete(job["job_id"], status="succeeded", reason=decision.reason)
            return {
                "job_id": job["job_id"],
                "status": "succeeded",
                "reason": decision.reason,
                "result": result,
            }
        except Exception as exc:  # noqa: BLE001
            terminal = (
                isinstance(exc, AriadneError)
                and exc.error.code
                in {
                    "ARIADNE_MEMORY_PROJECTOR_PROTOCOL",
                    "ARIADNE_MEMORY_PROJECTOR_UNAVAILABLE",
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "ARIADNE_MEMORY_CONFLICT",
                }
            )
            status = (
                "failed"
                if terminal or int(job.get("attempts") or 0) >= self.max_attempts
                else "pending"
            )
            error_text = (
                f"{exc.error.code}: {exc.error.message}"
                if isinstance(exc, AriadneError)
                else f"{type(exc).__name__}: {exc}"
            )
            self.complete(job["job_id"], status=status, error=error_text)
            return {"job_id": job["job_id"], "status": status, "error": error_text}

    async def drain(self, projector: ProjectorFn, *, max_jobs: int = 20) -> list[dict[str, Any]]:
        results = []
        for _ in range(max_jobs):
            item = await self.process_one(projector)
            if item is None:
                break
            results.append(item)
        return results
