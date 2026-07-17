from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

ENTRY_LIMIT = 24
ENTRY_CHAR_LIMIT = 600


@dataclass
class CuratedStore:
    path: Path
    entry_limit: int = ENTRY_LIMIT
    entry_char_limit: int = ENTRY_CHAR_LIMIT

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"user": [], "session": {}})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _entries(self, data: dict[str, Any], *, scope: str, session_id: str) -> list[dict[str, str]]:
        if scope == "user":
            return list(data.get("user") or [])
        sessions = data.setdefault("session", {})
        return list(sessions.get(session_id) or [])

    def _set_entries(
        self, data: dict[str, Any], *, scope: str, session_id: str, entries: list[dict[str, str]]
    ) -> None:
        if scope == "user":
            data["user"] = entries
        else:
            sessions = data.setdefault("session", {})
            sessions[session_id] = entries

    def snapshot_text(self, *, session_id: str) -> tuple[str, int]:
        data = self._read()
        user = self._entries(data, scope="user", session_id=session_id)
        sess = self._entries(data, scope="session", session_id=session_id)
        lines: list[str] = []
        if user:
            lines.append("[CURATED_DURABLE user]")
            for idx, item in enumerate(user, start=1):
                lines.append(f"{idx}. {item['content']}")
        if sess:
            lines.append("[CURATED_DURABLE session]")
            for idx, item in enumerate(sess, start=1):
                lines.append(f"{idx}. {item['content']}")
        text = "\n".join(lines)
        return text, len(user) + len(sess)

    def apply(
        self,
        *,
        action: str,
        content: str = "",
        entry_ref: str = "",
        scope: str = "user",
        session_id: str,
    ) -> dict[str, Any]:
        action = (action or "").strip().lower()
        scope = (scope or "user").strip().lower()
        if scope not in {"user", "session"}:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "scope must be user|session"))
        if action not in {"add", "update", "remove", "read"}:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "action must be add|update|remove|read")
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
                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "content is required"))
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
            entries.append({"id": f"e{len(entries)+1}", "content": clean})
        elif action == "update":
            idx = self._resolve_ref(entries, entry_ref)
            entries[idx] = {"id": entries[idx]["id"], "content": clean}
        elif action == "remove":
            idx = self._resolve_ref(entries, entry_ref or clean)
            entries.pop(idx)
        # renumber ids
        for i, item in enumerate(entries, start=1):
            item["id"] = f"e{i}"
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

    def _resolve_ref(self, entries: list[dict[str, str]], ref: str) -> int:
        token = (ref or "").strip().lower()
        if not token:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "entry_ref is required"))
        if token.isdigit():
            idx = int(token) - 1
            if 0 <= idx < len(entries):
                return idx
        for i, item in enumerate(entries):
            if item.get("id", "").lower() == token:
                return i
            if item.get("content", "").lower() == token:
                return i
        raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", f"entry not found: {ref}"))
