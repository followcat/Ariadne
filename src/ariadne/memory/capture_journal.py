from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .episodes import EPISODE_EVENT_TYPES
from .json_file import locked_read_json, locked_update_json, locked_write_json
from .limits import (
    MAX_CAPTURE_RECORDS,
    MAX_CAPTURE_RESUME_BATCH_SIZE,
    validate_capacity,
)

CAPTURE_STAGES = (
    "user_model",
    "state",
    "episode",
    "reflection",
    "prospective",
)
# Backward-compatible public name; the limit contract has one source of truth.
CAPTURE_RESUME_LIMIT_MAX = MAX_CAPTURE_RESUME_BATCH_SIZE
CAPTURE_QUARANTINE_LIMIT_MAX = 512
CAPTURE_JOURNAL_SCHEMA_VERSION = 2


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
        self.max_records = validate_capacity(
            self.max_records,
            field="capture_max_records",
            maximum=MAX_CAPTURE_RECORDS,
        )
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, self._empty())
        else:
            self._migrate_schema()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_JOURNAL_SCHEMA_VERSION,
            "records": {},
            "quarantined_records": {},
        }

    @staticmethod
    def _quarantine_row(
        *,
        capture_id: str,
        row: dict[str, Any] | None,
        reason: str,
        legacy_status: str,
        now: float,
        source_schema_version: int = 1,
    ) -> dict[str, Any]:
        out = copy.deepcopy(row) if isinstance(row, dict) else {}
        out["capture_id"] = capture_id
        out["legacy_status"] = legacy_status
        out["status"] = "migration_required"
        out["migration_error_code"] = "ARIADNE_MEMORY_CAPTURE_MIGRATION_REQUIRED"
        out["migration_reason"] = reason
        out["source_schema_version"] = source_schema_version
        out["quarantined_at"] = now
        return out

    @staticmethod
    def _event_contract_error(event: Any, index: int) -> str | None:
        if not isinstance(event, dict):
            return f"prepared event {index} is not an object"
        event_type = str(event.get("type") or "")
        if event_type not in EPISODE_EVENT_TYPES:
            return f"prepared event {index} has invalid type"
        content = event.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 2000:
            return f"prepared event {index} has invalid content"
        evidence = event.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            return f"prepared event {index} has no evidence"
        for ref_index, ref in enumerate(evidence):
            if not isinstance(ref, dict):
                return f"prepared event {index} evidence {ref_index} is not an object"
            for key in ("session_id", "turn_id", "source", "quote"):
                if not isinstance(ref.get(key), str) or not ref[key].strip():
                    return f"prepared event {index} evidence {ref_index} has invalid {key}"
        if not isinstance(event.get("reason", ""), str):
            return f"prepared event {index} has invalid reason"
        if not isinstance(event.get("metadata", {}), dict):
            return f"prepared event {index} has invalid metadata"
        entities = event.get("entities", [])
        if not isinstance(entities, list) or not all(
            isinstance(item, str) for item in entities
        ):
            return f"prepared event {index} has invalid entities"
        relation = event.get("relation")
        if relation is not None and (
            not isinstance(relation, dict)
            or not isinstance(relation.get("from"), str)
            or not isinstance(relation.get("to"), str)
        ):
            return f"prepared event {index} has invalid relation"
        return None

    @classmethod
    def _prepared_contract_error(cls, prepared: Any) -> str | None:
        if not isinstance(prepared, dict):
            return "pending capture prepared payload is not an object"
        events = prepared.get("events")
        if not isinstance(events, list):
            return "pending capture prepared.events is not a list"
        for index, event in enumerate(events):
            error = cls._event_contract_error(event, index)
            if error is not None:
                return error
        reflection_signals = prepared.get("reflection_signals", [])
        if not isinstance(reflection_signals, list) or not all(
            isinstance(item, dict) for item in reflection_signals
        ):
            return "pending capture prepared.reflection_signals is invalid"
        prospective_specs = prepared.get("prospective_specs", [])
        if not isinstance(prospective_specs, list):
            return "pending capture prepared.prospective_specs is not a list"
        for index, spec in enumerate(prospective_specs):
            if not isinstance(spec, dict):
                return f"pending capture prospective spec {index} is not an object"
            content = spec.get("content")
            trigger = spec.get("trigger")
            if not isinstance(content, str) or not content.strip() or len(content) > 1000:
                return f"pending capture prospective spec {index} has invalid content"
            if not isinstance(trigger, dict) or not trigger:
                return f"pending capture prospective spec {index} has invalid trigger"
        context = prepared.get("prospective_context", {})
        if not isinstance(context, dict):
            return "pending capture prospective_context is not an object"
        for key in ("workspace", "text"):
            if not isinstance(context.get(key, ""), str):
                return f"pending capture prospective_context.{key} is invalid"
        for key in ("changed_paths", "tool_names", "event_types", "entity_ids"):
            values = context.get(key, [])
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                return f"pending capture prospective_context.{key} is invalid"
        if not isinstance(prepared.get("llm_used", False), bool):
            return "pending capture llm_used is invalid"
        rejected = prepared.get("llm_rejected", 0)
        if not isinstance(rejected, int) or rejected < 0:
            return "pending capture llm_rejected is invalid"
        return None

    @staticmethod
    def _stage_result_contract_error(stage: str, result: Any) -> str | None:
        if not isinstance(result, dict):
            return f"capture stage {stage} result is not an object"
        list_fields = {
            "user_model": ("user_model_entry_ids",),
            "episode": ("event_ids",),
            "reflection": ("reflection_candidate_ids",),
            "prospective": ("prospective_entry_ids", "triggered_prospective_ids"),
        }.get(stage, ())
        for field in list_fields:
            values = result.get(field)
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                return f"capture stage {stage} result.{field} is invalid"
        if stage == "state" and (
            "state_version" not in result
            or result.get("state_version") is not None
            and not isinstance(result.get("state_version"), int)
        ):
            return "capture stage state result.state_version is invalid"
        return None

    @classmethod
    def _stages_contract_error(cls, stages: Any) -> str | None:
        if not isinstance(stages, dict):
            return "pending capture stages is not an object"
        for stage in CAPTURE_STAGES:
            row = stages.get(stage)
            if not isinstance(row, dict):
                return f"pending capture is missing stage {stage}"
            status = row.get("status")
            if status not in {"pending", "done"}:
                return f"pending capture stage {stage} has invalid status"
            if status == "done":
                error = cls._stage_result_contract_error(stage, row.get("result", {}))
                if error is not None:
                    return error
        return None

    @classmethod
    def _record_contract_error(
        cls, capture_id: str, row: dict[str, Any]
    ) -> str | None:
        """Return why a journal row cannot satisfy _resume_record prerequisites."""

        status = str(row.get("status") or "")
        # workspace_key may be "" for the default personal store.
        if "workspace_key" not in row:
            return "legacy record is missing workspace_key"
        workspace_key = str(row.get("workspace_key") or "")
        session_id = str(row.get("session_id") or "")
        turn_id = str(row.get("turn_id") or "")
        if not session_id or not turn_id:
            return "legacy record is missing session/turn identity"
        expected_id = cls.capture_id(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
        )
        row_id = str(row.get("capture_id") or capture_id)
        if row_id != capture_id or expected_id != capture_id:
            return "legacy capture_id does not match workspace/session/turn"
        if status == "completed":
            # Completed rows may lack prepared plan; replay returns stored report.
            if not isinstance(row.get("report"), dict):
                return "legacy completed capture is missing report"
            return None
        if status != "in_progress":
            return "legacy record status is unrecoverable"
        if not str(row.get("state_store_identity") or "").strip():
            return "legacy pending capture has no state-store identity"
        if not str(row.get("input_digest") or "").strip():
            return "pending capture is missing input_digest"
        prepared_error = cls._prepared_contract_error(row.get("prepared"))
        if prepared_error is not None:
            return prepared_error
        return cls._stages_contract_error(row.get("stages"))

    def _migrate_schema(self) -> None:
        """Upgrade v1 without guessing affinity for unrecoverable pending rows."""

        def mut(data: Any) -> dict[str, Any]:
            if not isinstance(data, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "automatic-capture journal root must be an object",
                    )
                )
            version = int(data.get("schema_version") or 0)
            if version == CAPTURE_JOURNAL_SCHEMA_VERSION:
                if not isinstance(data.get("records"), dict) or not isinstance(
                    data.get("quarantined_records"), dict
                ):
                    raise AriadneError(
                        app_error(
                            "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                            "automatic-capture journal containers are invalid",
                        )
                    )
                active: dict[str, Any] = {}
                quarantined = copy.deepcopy(data.get("quarantined_records") or {})
                now = time.time()
                for key, value in (data.get("records") or {}).items():
                    capture_id = str(key)
                    if not isinstance(value, dict):
                        quarantined[capture_id] = self._quarantine_row(
                            capture_id=capture_id,
                            row=None,
                            reason="v2 record is not an object",
                            legacy_status="invalid",
                            now=now,
                            source_schema_version=version,
                        )
                        continue
                    row = copy.deepcopy(value)
                    error = self._record_contract_error(capture_id, row)
                    if error is not None:
                        quarantined[capture_id] = self._quarantine_row(
                            capture_id=capture_id,
                            row=row,
                            reason=error,
                            legacy_status=str(row.get("status") or "missing"),
                            now=now,
                            source_schema_version=version,
                        )
                        continue
                    active[capture_id] = row
                return {
                    **data,
                    "records": active,
                    "quarantined_records": quarantined,
                }
            if version != 1:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "unknown automatic-capture journal schema",
                        schema_version=version,
                    )
                )
            records = data.get("records") or {}
            if not isinstance(records, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "legacy automatic-capture records must be an object",
                    )
                )
            active: dict[str, Any] = {}
            quarantined: dict[str, Any] = {}
            now = time.time()
            for key, value in records.items():
                capture_id = str(key)
                if not isinstance(value, dict):
                    quarantined[capture_id] = self._quarantine_row(
                        capture_id=capture_id,
                        row=None,
                        reason="legacy record is not an object",
                        legacy_status="invalid",
                        now=now,
                    )
                    continue
                row = copy.deepcopy(value)
                status = str(row.get("status") or "")
                contract_error = self._record_contract_error(capture_id, row)
                if contract_error is not None:
                    quarantined[capture_id] = self._quarantine_row(
                        capture_id=capture_id,
                        row=row,
                        reason=contract_error,
                        legacy_status=status or "missing",
                        now=now,
                        source_schema_version=version,
                    )
                    continue
                row["source_schema_version"] = 1
                # Completed rows without identity stay replayable by input_digest.
                if status == "completed" and not str(
                    row.get("state_store_identity") or ""
                ).strip():
                    row["state_store_identity"] = ""
                active[capture_id] = row
            return {
                "schema_version": CAPTURE_JOURNAL_SCHEMA_VERSION,
                "records": active,
                "quarantined_records": quarantined,
                "migrated_from_schema_version": 1,
                "migrated_at": now,
            }

        locked_update_json(self.path, mut, default=self._empty())

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
        if (
            not isinstance(data, dict)
            or int(data.get("schema_version") or 0)
            != CAPTURE_JOURNAL_SCHEMA_VERSION
            or not isinstance(data.get("records"), dict)
            or not isinstance(data.get("quarantined_records"), dict)
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    "unknown automatic-capture journal schema",
                )
            )
        return data

    def list_quarantined(
        self,
        *,
        workspace_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > CAPTURE_QUARANTINE_LIMIT_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    "capture quarantine limit is outside the supported range",
                    limit=limit,
                    maximum=CAPTURE_QUARANTINE_LIMIT_MAX,
                )
            )
        rows = [
            copy.deepcopy(row)
            for row in (self._read().get("quarantined_records") or {}).values()
            if isinstance(row, dict)
            and (
                workspace_key is None
                or str(row.get("workspace_key") or "") == workspace_key
            )
        ]
        rows.sort(
            key=lambda row: (
                float(row.get("quarantined_at") or 0.0),
                str(row.get("capture_id") or ""),
            )
        )
        return rows[:limit]

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

    def list_pending(
        self, *, workspace_key: str, limit: int = 4
    ) -> list[dict[str, Any]]:
        """Return a bounded, fair batch of incomplete captures.

        Oldest ``updated_at`` wins. Recording a failed resume advances that
        timestamp, rotating a persistently bad record behind other work.
        """

        if limit < 1 or limit > CAPTURE_RESUME_LIMIT_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    "capture resume limit is outside the supported range",
                    limit=limit,
                    maximum=CAPTURE_RESUME_LIMIT_MAX,
                )
            )
        records = self._read().get("records") or {}
        pending = [
            copy.deepcopy(row)
            for row in records.values()
            if (
                isinstance(row, dict)
                and row.get("status") == "in_progress"
                and str(row.get("workspace_key") or "") == workspace_key
            )
        ]
        pending.sort(
            key=lambda row: (
                float(row.get("updated_at") or 0.0),
                float(row.get("created_at") or 0.0),
                str(row.get("capture_id") or ""),
            )
        )
        return pending[:limit]

    @classmethod
    def validate_record(cls, capture_id: str, row: dict[str, Any]) -> str | None:
        """Validate a record immediately before recovery, including v2 rows."""

        return cls._record_contract_error(capture_id, row)

    def quarantine(
        self,
        *,
        capture_id: str,
        reason: str,
        error_code: str = "ARIADNE_MEMORY_CAPTURE_MIGRATION_REQUIRED",
    ) -> dict[str, Any] | None:
        """Move an active row to terminal quarantine exactly once."""

        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            records = data.setdefault("records", {})
            quarantined = data.setdefault("quarantined_records", {})
            existing = quarantined.get(capture_id)
            if not isinstance(existing, dict):
                row = records.pop(capture_id, None)
                if not isinstance(row, dict):
                    return data
                quarantined[capture_id] = self._quarantine_row(
                    capture_id=capture_id,
                    row=row,
                    reason=reason,
                    legacy_status=str(row.get("status") or "missing"),
                    now=time.time(),
                    source_schema_version=CAPTURE_JOURNAL_SCHEMA_VERSION,
                )
                quarantined[capture_id]["migration_error_code"] = error_code
                existing = quarantined[capture_id]
            result.update(copy.deepcopy(existing))
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result or None

    def note_resume_failure(
        self,
        *,
        capture_id: str,
        error_code: str,
        error_message: str,
    ) -> dict[str, Any]:
        """Persist one failed recovery attempt and rotate the pending record."""

        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("records") or {}).get(capture_id)
            if not isinstance(row, dict) or row.get("status") != "in_progress":
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        "capture resume failure has no in-progress journal record",
                        capture_id=capture_id,
                    )
                )
            now = time.time()
            attempt = int(row.get("resume_attempts") or 0) + 1
            failure = {
                "attempt": attempt,
                "error_code": str(error_code)[:120],
                "error_message": str(error_message)[:500],
                "failed_at": now,
            }
            history = [
                copy.deepcopy(item)
                for item in row.get("resume_failures") or []
                if isinstance(item, dict)
            ][-15:]
            history.append(failure)
            row["resume_attempts"] = attempt
            row["last_resume_failure"] = copy.deepcopy(failure)
            row["resume_failures"] = history
            row["updated_at"] = now
            result.update(copy.deepcopy(failure))
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    def start(
        self,
        *,
        workspace_key: str,
        session_id: str,
        turn_id: str,
        input_digest: str,
        state_store_identity: str,
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        capture_id = self.capture_id(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
        )
        result: dict[str, Any] = {}
        if not state_store_identity.strip():
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_AFFINITY",
                    "automatic capture requires a state-store identity",
                )
            )

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
                stored_identity = str(current.get("state_store_identity") or "").strip()
                if current.get("status") == "completed":
                    # Legacy completed rows may lack affinity; digest match is enough.
                    if stored_identity and stored_identity != state_store_identity:
                        raise AriadneError(
                            app_error(
                                "ARIADNE_MEMORY_CAPTURE_AFFINITY",
                                "automatic capture state-store identity changed",
                                capture_id=capture_id,
                            )
                        )
                elif stored_identity != state_store_identity:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_MEMORY_CAPTURE_AFFINITY",
                            "automatic capture state-store identity changed",
                            capture_id=capture_id,
                        )
                    )
                result.update(copy.deepcopy(current))
                result["idempotent_replay"] = True
                return data
            quarantined = (data.get("quarantined_records") or {}).get(capture_id)
            if isinstance(quarantined, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_MIGRATION_REQUIRED",
                        "capture id is terminally quarantined and cannot be replayed",
                        capture_id=capture_id,
                    )
                )
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
                "state_store_identity": state_store_identity,
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
            result_error = self._stage_result_contract_error(stage, stage_result)
            if result_error is not None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                        result_error,
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
