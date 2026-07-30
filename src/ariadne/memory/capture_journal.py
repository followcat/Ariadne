from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

CAPTURE_STAGES = (
    "user_model",
    "state",
    "episode",
    "reflection",
    "prospective",
)


@dataclass(slots=True)
class CaptureJournalStore:
    """Recoverable turn-capture coordinator over independently locked stores.

    This is intentionally not presented as a multi-file ACID transaction. The
    journal records durable stage progress, while each target store accepts a
    turn-scoped idempotency key so a crash between a store write and the stage
    marker can be replayed safely.
    """

    path: Path
    max_records: int = 4096

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 1, "records": {}}

    @staticmethod
    def capture_id(*, workspace_key: str, session_id: str, turn_id: str) -> str:
        digest = hashlib.sha256(
            f"capture-v1\x1f{workspace_key}\x1f{session_id}\x1f{turn_id}".encode(
                "utf-8"
            )
        ).hexdigest()[:20]
        return f"capture-{digest}"

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default=self._empty())
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    "unknown automatic-capture journal schema",
                )
            )
        return data

    def get(
        self, *, workspace_key: str, session_id: str, turn_id: str
    ) -> dict[str, Any] | None:
        capture_id = self.capture_id(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
        )
        row = (self._read().get("records") or {}).get(capture_id)
        return copy.deepcopy(row) if isinstance(row, dict) else None

    def start(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        input_digest: str,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        capture_id = self.capture_id(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
        )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            records = data.setdefault("records", {})
            current = records.get(capture_id)
            if current is not None:
                if current.get("input_digest") != input_digest:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_MEMORY_CAPTURE_CONFLICT",
                            "one turn id cannot be captured with different evidence",
                            capture_id=capture_id,
                        )
                    )
                result.update(copy.deepcopy(current))
                result["idempotent_replay"] = True
                return data
            if len(records) >= self.max_records:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_CAPACITY",
                        "automatic-capture journal capacity exceeded",
                    )
                )
            now = time.time()
            row = {
                "capture_id": capture_id,
                "workspace_key": workspace_key,
                "session_id": session_id,
                "turn_id": turn_id,
                "input_digest": input_digest,
                "status": "in_progress",
                "prepared": copy.deepcopy(prepared),
                "stages": {
                    stage: {"status": "pending", "result": {}}
                    for stage in CAPTURE_STAGES
                },
                "report": {},
                "created_at": now,
                "updated_at": now,
            }
            records[capture_id] = row
            result.update(copy.deepcopy(row))
            result["idempotent_replay"] = False
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    def mark_stage(
        self, *, capture_id: str, stage: str, stage_result: dict[str, Any]
    ) -> dict[str, Any]:
        if stage not in CAPTURE_STAGES:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    f"unknown automatic-capture stage: {stage}",
                )
            )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("records") or {}).get(capture_id)
            if row is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "automatic-capture stage has no journal record",
                        capture_id=capture_id,
                    )
                )
            current = (row.get("stages") or {}).get(stage)
            if not isinstance(current, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "automatic-capture journal is missing a stage",
                        capture_id=capture_id,
                        stage=stage,
                    )
                )
            if current.get("status") == "done":
                result.update(copy.deepcopy(current))
                result["idempotent_replay"] = True
                return data
            current["status"] = "done"
            current["result"] = copy.deepcopy(stage_result)
            current["completed_at"] = time.time()
            row["updated_at"] = current["completed_at"]
            result.update(copy.deepcopy(current))
            result["idempotent_replay"] = False
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    def complete(self, *, capture_id: str, report: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("records") or {}).get(capture_id)
            if row is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "automatic capture has no journal record",
                        capture_id=capture_id,
                    )
                )
            incomplete = [
                stage
                for stage in CAPTURE_STAGES
                if ((row.get("stages") or {}).get(stage) or {}).get("status")
                != "done"
            ]
            if incomplete:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "automatic capture cannot complete before every stage",
                        capture_id=capture_id,
                        incomplete=incomplete,
                    )
                )
            if row.get("status") == "completed":
                result.update(copy.deepcopy(row.get("report") or {}))
                result["idempotent_replay"] = True
                return data
            row["status"] = "completed"
            row["report"] = copy.deepcopy(report)
            row["completed_at"] = time.time()
            row["updated_at"] = row["completed_at"]
            result.update(copy.deepcopy(report))
            result["idempotent_replay"] = False
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result
