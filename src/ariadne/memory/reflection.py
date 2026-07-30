from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json
from .user_model import UserModelStore


@dataclass(slots=True)
class ReflectionStore:
    """Cross-session pattern candidates; inferred records require confirmation."""

    path: Path
    min_distinct_sessions: int = 3

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.min_distinct_sessions < 2:
            raise AriadneError(
                app_error(
                    "ARIADNE_REFLECTION_INVALID",
                    "reflection threshold must be at least two distinct sessions",
                )
            )
        if not self.path.exists():
            locked_write_json(self.path, self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"schema_version": 1, "signals": {}, "candidates": {}}

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default=self._empty())
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_REFLECTION_INVALID", "unknown reflection schema")
            )
        return data

    def observe(
        self,
        *,
        session_id: str,
        turn_id: str,
        signals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        sid = (session_id or "").strip()
        tid = (turn_id or "").strip()
        if not sid or not tid:
            raise AriadneError(
                app_error(
                    "ARIADNE_REFLECTION_INVALID",
                    "reflection evidence requires session_id and turn_id",
                )
            )
        created_or_updated: list[dict[str, Any]] = []

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            ledger = data.setdefault("signals", {})
            candidates = data.setdefault("candidates", {})
            for signal in signals:
                if signal.get("explicit_durable"):
                    continue
                entry_type = str(signal.get("entry_type") or "preference").strip()
                key = str(signal.get("key") or "").strip()
                value = signal.get("value")
                quote = str(signal.get("evidence_quote") or "").strip()
                if not key or value in (None, "") or not quote:
                    continue
                logical = f"{entry_type}:{key}:{value!r}"
                signal_id = hashlib.sha256(
                    f"{sid}:{tid}:{logical}".encode("utf-8")
                ).hexdigest()[:20]
                ledger.setdefault(
                    signal_id,
                    {
                        "signal_id": signal_id,
                        "entry_type": entry_type,
                        "key": key,
                        "value": value,
                        "session_id": sid,
                        "turn_id": tid,
                        "evidence_quote": quote[:400],
                        "observed_at": time.time(),
                    },
                )
                matching = [
                    row
                    for row in ledger.values()
                    if row.get("entry_type") == entry_type
                    and row.get("key") == key
                    and row.get("value") == value
                ]
                sessions = sorted(
                    {str(row.get("session_id") or "") for row in matching if row.get("session_id")}
                )
                if len(sessions) < self.min_distinct_sessions:
                    continue
                candidate_id = "reflection-" + hashlib.sha256(
                    logical.encode("utf-8")
                ).hexdigest()[:16]
                current = candidates.get(candidate_id)
                if current is not None and current.get("status") in {"accepted", "rejected"}:
                    continue
                row = {
                    "candidate_id": candidate_id,
                    "status": "pending",
                    "entry_type": entry_type,
                    "key": key,
                    "value": value,
                    "scope": str(signal.get("scope") or "user"),
                    "observation_count": len(matching),
                    "session_count": len(sessions),
                    "session_ids": sessions,
                    "evidence": [
                        {
                            "session_id": item.get("session_id"),
                            "turn_id": item.get("turn_id"),
                            "quote": item.get("evidence_quote"),
                        }
                        for item in matching[-16:]
                    ],
                    "created_at": float((current or {}).get("created_at") or time.time()),
                    "updated_at": time.time(),
                }
                candidates[candidate_id] = row
                created_or_updated.append(copy.deepcopy(row))
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return created_or_updated

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in {"pending", "accepted", "rejected"}:
            raise AriadneError(
                app_error("ARIADNE_REFLECTION_INVALID", f"unknown status: {status}")
            )
        rows = []
        for row in (self._read().get("candidates") or {}).values():
            if status is not None and row.get("status") != status:
                continue
            rows.append(copy.deepcopy(row))
        rows.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=True)
        return rows

    def decide(
        self,
        *,
        candidate_id: str,
        action: str,
        user_model: UserModelStore | None = None,
        workspace_key: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        action_n = (action or "").strip().lower()
        if action_n not in {"accept", "reject"}:
            raise AriadneError(
                app_error("ARIADNE_REFLECTION_INVALID", "action must be accept|reject")
            )
        candidate = next(
            (row for row in self.list() if row.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            raise AriadneError(
                app_error(
                    "ARIADNE_REFLECTION_NOT_FOUND",
                    f"reflection candidate not found: {candidate_id}",
                )
            )
        if candidate.get("status") != "pending":
            return candidate
        promoted: dict[str, Any] | None = None
        if action_n == "accept":
            if user_model is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "accepting reflection requires a user model store",
                    )
                )
            scope = str(candidate.get("scope") or "user")
            promoted = user_model.upsert_by_key(
                entry_type=str(candidate.get("entry_type") or "preference"),
                key=str(candidate.get("key") or ""),
                value=candidate.get("value"),
                source="model_inferred",
                confidence=min(0.95, 0.6 + 0.05 * int(candidate.get("session_count") or 0)),
                scope=scope,
                workspace_key=workspace_key if scope == "workspace" else "",
                session_id=session_id if scope == "session" else "",
                change_reason=f"accepted reflection {candidate_id}",
                evidence=list(candidate.get("evidence") or []),
            )

        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            row = (data.get("candidates") or {}).get(candidate_id)
            if row is None:
                raise AriadneError(
                    app_error("ARIADNE_REFLECTION_NOT_FOUND", "candidate disappeared")
                )
            row["status"] = "accepted" if action_n == "accept" else "rejected"
            row["decided_at"] = time.time()
            if promoted is not None:
                row["promoted_entry_id"] = promoted.get("entry_id")
            result.update(copy.deepcopy(row))
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    def render_pending(self) -> tuple[str, int]:
        rows = self.list(status="pending")
        if not rows:
            return "", 0
        lines = ["[REFLECTION_CANDIDATES: USER CONFIRMATION REQUIRED]"]
        for row in rows[:12]:
            lines.append(
                f"- {row['candidate_id']}: observed {row['key']}={row['value']!r} "
                f"across {row['session_count']} sessions. Ask before accepting."
            )
        return "\n".join(lines), len(rows)
