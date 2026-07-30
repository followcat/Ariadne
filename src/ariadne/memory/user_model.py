from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

USER_MODEL_TYPES = {"preference", "goal", "capability", "constraint", "relation"}
USER_MODEL_SOURCES = {"user_explicit", "tool_observed", "model_inferred", "imported"}
USER_MODEL_SCOPES = {"user", "workspace", "session"}


@dataclass(slots=True)
class UserModelStore:
    """Editable typed long-lived personalization with revisioned history."""

    path: Path
    max_entries: int = 512

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, {"schema_version": 1, "entries": {}})

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(
            self.path, default={"schema_version": 1, "entries": {}}
        )
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", "unknown user model schema")
            )
        return data

    @staticmethod
    def _validate(
        *,
        entry_type: str,
        key: str,
        source: str,
        confidence: float,
        scope: str,
        workspace_key: str,
        session_id: str,
    ) -> None:
        if entry_type not in USER_MODEL_TYPES:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", f"unknown user model type: {entry_type}")
            )
        if source not in USER_MODEL_SOURCES:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", f"unknown user model source: {source}")
            )
        if scope not in USER_MODEL_SCOPES:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", f"unknown user model scope: {scope}")
            )
        if not key.strip() or len(key) > 128:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", "key must be 1..128 characters")
            )
        if not 0.0 <= confidence <= 1.0:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", "confidence must be between 0 and 1")
            )
        if scope == "workspace" and not workspace_key.strip():
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", "workspace scope requires workspace_key")
            )
        if scope == "session" and not session_id.strip():
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", "session scope requires session_id")
            )

    def upsert(
        self,
        *,
        entry_type: str,
        key: str,
        value: Any,
        source: str,
        confidence: float,
        scope: str,
        workspace_key: str = "",
        session_id: str = "",
        source_turn_id: str = "",
        entry_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._validate(
            entry_type=entry_type,
            key=key,
            source=source,
            confidence=float(confidence),
            scope=scope,
            workspace_key=workspace_key,
            session_id=session_id,
        )
        eid = (entry_id or uuid.uuid4().hex[:16]).strip()
        now = time.time()
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            entries = data.setdefault("entries", {})
            current = entries.get(eid)
            if current is None and entry_id is not None:
                raise AriadneError(
                    app_error("ARIADNE_USER_MODEL_NOT_FOUND", f"entry not found: {eid}")
                )
            if current is None and len(entries) >= self.max_entries:
                raise AriadneError(
                    app_error("ARIADNE_USER_MODEL_CAPACITY", "user model capacity exceeded")
                )
            current_revision = int((current or {}).get("revision") or 0)
            if expected_revision is not None and expected_revision != current_revision:
                raise AriadneError(
                    app_error(
                        "ARIADNE_USER_MODEL_CONFLICT",
                        "user model revision conflict",
                        entry_id=eid,
                        expected_revision=expected_revision,
                        current_revision=current_revision,
                    )
                )
            history = [copy.deepcopy(row) for row in ((current or {}).get("history") or [])]
            if current is not None:
                snapshot = {
                    field: copy.deepcopy(item)
                    for field, item in current.items()
                    if field != "history"
                }
                snapshot["status"] = "superseded"
                history.append(snapshot)
            row = {
                "entry_id": eid,
                "type": entry_type,
                "key": key.strip(),
                "value": value,
                "source": source,
                "confidence": float(confidence),
                "scope": scope,
                "workspace_key": workspace_key if scope == "workspace" else "",
                "session_id": session_id if scope == "session" else "",
                "source_turn_id": source_turn_id,
                "status": "active",
                "revision": current_revision + 1,
                "created_at": float((current or {}).get("created_at") or now),
                "updated_at": now,
                "history": history,
            }
            entries[eid] = row
            result.update(copy.deepcopy(row))
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "entries": {}})
        return result

    def expire(
        self, *, entry_id: str, expected_revision: int, source: str = "user_explicit"
    ) -> dict[str, Any]:
        if source not in USER_MODEL_SOURCES:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_INVALID", f"unknown source: {source}")
            )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("entries") or {}).get(entry_id)
            if row is None:
                raise AriadneError(
                    app_error("ARIADNE_USER_MODEL_NOT_FOUND", f"entry not found: {entry_id}")
                )
            revision = int(row.get("revision") or 0)
            if revision != expected_revision:
                raise AriadneError(
                    app_error(
                        "ARIADNE_USER_MODEL_CONFLICT",
                        "user model revision conflict",
                        entry_id=entry_id,
                        expected_revision=expected_revision,
                        current_revision=revision,
                    )
                )
            history = [copy.deepcopy(item) for item in row.get("history") or []]
            snapshot = {key: copy.deepcopy(value) for key, value in row.items() if key != "history"}
            snapshot["status"] = "superseded"
            history.append(snapshot)
            row.update(
                {
                    "status": "expired",
                    "source": source,
                    "revision": revision + 1,
                    "updated_at": time.time(),
                    "history": history,
                }
            )
            result.update(copy.deepcopy(row))
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "entries": {}})
        return result

    def list(
        self,
        *,
        workspace_key: str = "",
        session_id: str = "",
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        rows = []
        for row in (self._read().get("entries") or {}).values():
            if not include_expired and row.get("status") != "active":
                continue
            scope = row.get("scope")
            if scope == "workspace" and row.get("workspace_key") != workspace_key:
                continue
            if scope == "session" and row.get("session_id") != session_id:
                continue
            rows.append(copy.deepcopy(row))
        rows.sort(key=lambda row: (str(row.get("type")), str(row.get("key"))))
        return rows

    def render(self, *, workspace_key: str = "", session_id: str = "") -> tuple[str, int]:
        rows = self.list(workspace_key=workspace_key, session_id=session_id)
        if not rows:
            return "", 0
        lines = ["[USER_MODEL: USER-EDITABLE TYPED PERSONALIZATION]"]
        for row in rows:
            lines.append(
                f"- {row['type']}:{row['key']}={row['value']!r} "
                f"scope={row['scope']} confidence={row['confidence']:.2f} "
                f"source={row['source']} id={row['entry_id']} rev={row['revision']}"
            )
        text = "\n".join(lines)
        if len(text) > 6_000:
            raise AriadneError(
                app_error("ARIADNE_USER_MODEL_CAPACITY", "rendered user model exceeds hard cap")
            )
        return text, len(rows)
