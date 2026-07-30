"""Curated durable memory (L3) with stable entry IDs and scopes.

Scopes: ``user`` | ``workspace`` | ``session`` (design/memory-scopes.md).
IDs are stable UUIDs; delete does **not** renumber remaining entries.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

ENTRY_LIMIT = 24
ENTRY_CHAR_LIMIT = 600
SCOPES = frozenset({"user", "workspace", "session"})


@dataclass
class CuratedStore:
    path: Path
    entry_limit: int = ENTRY_LIMIT
    entry_char_limit: int = ENTRY_CHAR_LIMIT

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"user": [], "workspace": [], "session": {}})
        else:
            self._migrate_if_needed()

    def _migrate_if_needed(self) -> None:
        data = self._read()
        changed = False
        if "workspace" not in data:
            data["workspace"] = []
            changed = True
        for scope_key in ("user", "workspace"):
            items = list(data.get(scope_key) or [])
            new_items, ch = self._stabilize_entries(items)
            if ch:
                data[scope_key] = new_items
                changed = True
        sessions = data.setdefault("session", {})
        for sid, items in list(sessions.items()):
            new_items, ch = self._stabilize_entries(list(items or []))
            if ch:
                sessions[sid] = new_items
                changed = True
        if changed:
            self._write(data)

    @staticmethod
    def _stabilize_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        changed = False
        out: list[dict[str, Any]] = []
        for item in entries:
            row = dict(item)
            eid = str(row.get("id") or "").strip()
            # Legacy e1, e2 renumbering scheme → stable id once
            if not eid or re.fullmatch(r"e\d+", eid):
                row["id"] = uuid.uuid4().hex[:12]
                changed = True
            if "source_turn_id" not in row:
                row["source_turn_id"] = ""
                changed = True
            if "updated_at" not in row:
                row["updated_at"] = time.time()
                changed = True
            out.append(row)
        return out, changed

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _entries(
        self, data: dict[str, Any], *, scope: str, session_id: str
    ) -> list[dict[str, Any]]:
        if scope == "user":
            return list(data.get("user") or [])
        if scope == "workspace":
            return list(data.get("workspace") or [])
        sessions = data.setdefault("session", {})
        return list(sessions.get(session_id) or [])

    def _set_entries(
        self,
        data: dict[str, Any],
        *,
        scope: str,
        session_id: str,
        entries: list[dict[str, Any]],
    ) -> None:
        if scope == "user":
            data["user"] = entries
        elif scope == "workspace":
            data["workspace"] = entries
        else:
            sessions = data.setdefault("session", {})
            sessions[session_id] = entries

    def snapshot_text(self, *, session_id: str) -> tuple[str, int]:
        data = self._read()
        user = self._entries(data, scope="user", session_id=session_id)
        workspace = self._entries(data, scope="workspace", session_id=session_id)
        sess = self._entries(data, scope="session", session_id=session_id)
        lines: list[str] = []
        count = 0
        for label, items in (
            ("user", user),
            ("workspace", workspace),
            ("session", sess),
        ):
            if not items:
                continue
            lines.append(f"[CURATED_DURABLE {label}]")
            for item in items:
                eid = item.get("id", "?")
                lines.append(f"- ({eid}) {item['content']}")
                count += 1
        return "\n".join(lines), count

    def apply(
        self,
        *,
        action: str,
        content: str = "",
        entry_ref: str = "",
        scope: str = "user",
        session_id: str,
        source_turn_id: str = "",
        source_session_id: str = "",
    ) -> dict[str, Any]:
        action = (action or "").strip().lower()
        scope = (scope or "user").strip().lower()
        if scope not in SCOPES:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "scope must be user|workspace|session",
                )
            )
        if action not in {"add", "update", "remove", "read"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "action must be add|update|remove|read",
                )
            )
        data = self._read()
        entries = self._entries(data, scope=scope, session_id=session_id)
        if action == "read":
            return {
                "action": "read",
                "scope": scope,
                "entries": entries,
                "entry_count": len(entries),
                "entry_limit": self.entry_limit,
            }
        clean = (content or "").strip()
        if action in {"add", "update"}:
            if not clean:
                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "content is required")
                )
            if len(clean) > self.entry_char_limit:
                raise AriadneError(
                    app_error(
                        "ARIADNE_INVALID_TOOL_ARGS",
                        f"entry exceeds {self.entry_char_limit} chars",
                        entry_char_limit=self.entry_char_limit,
                    )
                )
        if action == "add":
            if len(entries) >= self.entry_limit:
                raise AriadneError(
                    app_error(
                        "ARIADNE_INVALID_TOOL_ARGS",
                        f"curated memory full ({self.entry_limit})",
                        entry_limit=self.entry_limit,
                        entry_count=len(entries),
                    )
                )
            entries.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "content": clean,
                    "source_turn_id": (source_turn_id or "").strip(),
                    "source_session_id": (source_session_id or session_id or "").strip(),
                    "updated_at": time.time(),
                }
            )
        elif action == "update":
            idx = self._resolve_ref(entries, entry_ref)
            prev = entries[idx]
            entries[idx] = {
                "id": prev["id"],
                "content": clean,
                "source_turn_id": (source_turn_id or prev.get("source_turn_id") or ""),
                "source_session_id": (
                    source_session_id
                    or prev.get("source_session_id")
                    or session_id
                    or ""
                ),
                "updated_at": time.time(),
            }
        elif action == "remove":
            idx = self._resolve_ref(entries, entry_ref or clean)
            entries.pop(idx)
        # Stable IDs: do not renumber after delete
        self._set_entries(data, scope=scope, session_id=session_id, entries=entries)
        self._write(data)
        return {
            "action": action,
            "scope": scope,
            "entries": entries,
            "entry_count": len(entries),
            "entry_limit": self.entry_limit,
            "message": f"curated memory {action} ok",
        }

    def _resolve_ref(self, entries: list[dict[str, Any]], ref: str) -> int:
        token = (ref or "").strip().lower()
        if not token:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "entry_ref is required")
            )
        # 1-based index still accepted for ergonomics
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(entries):
                return idx
        for i, item in enumerate(entries):
            if str(item.get("id", "")).lower() == token:
                return i
            if str(item.get("content", "")).lower() == token:
                return i
        raise AriadneError(
            app_error("ARIADNE_INVALID_TOOL_ARGS", f"entry not found: {ref}")
        )
