from __future__ import annotations

import copy
import fnmatch
import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

PROSPECTIVE_TRIGGER_FIELDS = {
    "workspace_equals",
    "path_glob",
    "text_contains",
    "tool_name",
    "event_type",
    "entity_id",
}
PROSPECTIVE_STATUSES = {"pending", "triggered", "completed", "cancelled"}


@dataclass(slots=True)
class ProspectiveMemoryStore:
    """Structured future reminders; external scheduling stays in the host."""

    path: Path
    max_entries: int = 256

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "entries": {},
            "idempotency_keys": {},
            "match_idempotency_keys": {},
        }

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default=self._empty())
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_PROSPECTIVE_INVALID", "unknown prospective schema")
            )
        return data

    @staticmethod
    def _validate_trigger(trigger: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(trigger, dict) or not trigger:
            raise AriadneError(
                app_error("ARIADNE_PROSPECTIVE_INVALID", "trigger must be a non-empty object")
            )
        unknown = sorted(set(trigger) - PROSPECTIVE_TRIGGER_FIELDS)
        if unknown:
            raise AriadneError(
                app_error(
                    "ARIADNE_PROSPECTIVE_INVALID",
                    "unknown prospective trigger fields",
                    unknown=unknown,
                )
            )
        normalized: dict[str, Any] = {}
        for key, value in trigger.items():
            if isinstance(value, list):
                values = [str(item).strip() for item in value if str(item).strip()]
                if values:
                    normalized[key] = values
            else:
                clean = str(value).strip()
                if clean:
                    normalized[key] = clean
        if not normalized:
            raise AriadneError(
                app_error("ARIADNE_PROSPECTIVE_INVALID", "trigger has no usable values")
            )
        return normalized

    def create(
        self,
        *,
        content: str,
        trigger: dict[str, Any],
        source_session_id: str = "",
        source_turn_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        message = (content or "").strip()
        if not message or len(message) > 1000:
            raise AriadneError(
                app_error(
                    "ARIADNE_PROSPECTIVE_INVALID",
                    "prospective content must be 1..1000 characters",
                )
            )
        trigger_n = self._validate_trigger(trigger)
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            entries = data.setdefault("entries", {})
            keys = data.setdefault("idempotency_keys", {})
            scoped_key = (idempotency_key or "").strip()
            if scoped_key and scoped_key in keys:
                row = entries.get(keys[scoped_key])
                if row is None:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_PROSPECTIVE_INVALID",
                            "idempotency key points to missing entry",
                        )
                    )
                result.update(copy.deepcopy(row))
                result["idempotent_replay"] = True
                return data
            if len(entries) >= self.max_entries:
                raise AriadneError(
                    app_error(
                        "ARIADNE_PROSPECTIVE_CAPACITY",
                        "prospective memory capacity exceeded",
                    )
                )
            entry_id = "prospective-" + uuid.uuid4().hex[:16]
            now = time.time()
            row = {
                "entry_id": entry_id,
                "content": message,
                "trigger": trigger_n,
                "status": "pending",
                "source_session_id": source_session_id,
                "source_turn_id": source_turn_id,
                "created_at": now,
                "updated_at": now,
                "triggered_at": None,
            }
            entries[entry_id] = row
            if scoped_key:
                keys[scoped_key] = entry_id
            result.update(copy.deepcopy(row))
            result["idempotent_replay"] = False
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in PROSPECTIVE_STATUSES:
            raise AriadneError(
                app_error("ARIADNE_PROSPECTIVE_INVALID", f"unknown status: {status}")
            )
        rows = []
        for row in (self._read().get("entries") or {}).values():
            if status is not None and row.get("status") != status:
                continue
            rows.append(copy.deepcopy(row))
        rows.sort(key=lambda row: float(row.get("created_at") or 0))
        return rows

    def transition(self, *, entry_id: str, action: str) -> dict[str, Any]:
        action_n = (action or "").strip().lower()
        status = {"cancel": "cancelled", "complete": "completed"}.get(action_n)
        if status is None:
            raise AriadneError(
                app_error("ARIADNE_PROSPECTIVE_INVALID", "action must be cancel|complete")
            )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("entries") or {}).get(entry_id)
            if row is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_PROSPECTIVE_NOT_FOUND",
                        f"prospective memory not found: {entry_id}",
                    )
                )
            row["status"] = status
            row["updated_at"] = time.time()
            result.update(copy.deepcopy(row))
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    @staticmethod
    def _as_values(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    @classmethod
    def _matches(cls, trigger: dict[str, Any], context: dict[str, Any]) -> bool:
        workspace = str(context.get("workspace") or "")
        text = str(context.get("text") or "").casefold()
        paths = [str(item) for item in context.get("changed_paths") or []]
        tools = {str(item) for item in context.get("tool_names") or []}
        event_types = {str(item) for item in context.get("event_types") or []}
        entities = {str(item) for item in context.get("entity_ids") or []}
        for key, expected in trigger.items():
            values = cls._as_values(expected)
            if key == "workspace_equals" and workspace not in values:
                return False
            if key == "text_contains" and not any(value.casefold() in text for value in values):
                return False
            if key == "path_glob" and not any(
                fnmatch.fnmatch(path, pattern) for path in paths for pattern in values
            ):
                return False
            if key == "tool_name" and not tools.intersection(values):
                return False
            if key == "event_type" and not event_types.intersection(values):
                return False
            if key == "entity_id" and not entities.intersection(values):
                return False
        return True

    def match(
        self, *, context: dict[str, Any], idempotency_key: str = ""
    ) -> list[dict[str, Any]]:
        triggered: list[dict[str, Any]] = []

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            entries = data.setdefault("entries", {})
            match_keys = data.setdefault("match_idempotency_keys", {})
            scoped_key = (idempotency_key or "").strip()
            if scoped_key and scoped_key in match_keys:
                for entry_id in match_keys[scoped_key]:
                    row = entries.get(entry_id)
                    if row is None:
                        raise AriadneError(
                            app_error(
                                "ARIADNE_PROSPECTIVE_INVALID",
                                "match idempotency key points to a missing entry",
                            )
                        )
                    triggered.append(copy.deepcopy(row))
                return data
            for row in entries.values():
                if row.get("status") != "pending":
                    continue
                if not self._matches(dict(row.get("trigger") or {}), context):
                    continue
                row["status"] = "triggered"
                row["triggered_at"] = time.time()
                row["updated_at"] = row["triggered_at"]
                digest = hashlib.sha256(
                    repr(sorted(context.items())).encode("utf-8")
                ).hexdigest()[:16]
                row["trigger_context_digest"] = digest
                triggered.append(copy.deepcopy(row))
            if scoped_key:
                match_keys[scoped_key] = [
                    str(row.get("entry_id") or "") for row in triggered
                ]
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return triggered

    def render_active(self) -> tuple[str, int]:
        # Pending reminders stay dormant until their trigger matches. Context
        # only receives triggered records, avoiding an always-on reminder dump.
        rows = self.list(status="triggered")
        if not rows:
            return "", 0
        lines = ["[PROSPECTIVE_MEMORY: FUTURE REMINDERS]"]
        for row in rows[:20]:
            lines.append(
                f"- [{row['status']}] {row['content']} "
                f"id={row['entry_id']} trigger={row['trigger']}"
            )
        return "\n".join(lines), len(rows)
