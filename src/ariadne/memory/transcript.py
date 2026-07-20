from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TranscriptStore:
    """L0 session transcript on disk (JSONL)."""

    path: Path
    recent_limit: int = 8

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def recent_messages(
        self, *, limit: int | None = None, session_id: str | None = None
    ) -> list[dict[str, str]]:
        lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        window = limit if limit is not None else self.recent_limit
        messages: list[dict[str, str]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is not None:
                rec_sid = item.get("session_id")
                # Prefer stamped isolation. Legacy unstamped lines are kept only when
                # the store has never stamped any id (single-session file).
                if rec_sid is not None and str(rec_sid) != session_id:
                    continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages[-window:] if window else messages

    def all_records(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is not None and rec.get("session_id") not in (None, session_id):
                continue
            out.append(rec)
        return out

    def records_after(
        self, turn_id: str | None, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Records newer than the given turn (transcript order).

        Empty when turn_id is None or is the last turn recorded. If the turn id
        is not present in this transcript, all records are newer by definition.
        """
        records = self.all_records(session_id=session_id)
        if turn_id is None:
            return []
        last_idx = -1
        for idx, rec in enumerate(records):
            if str(rec.get("turn_id") or "") == turn_id:
                last_idx = idx
        return records[last_idx + 1 :]

    def ordered_turn_ids(self, *, session_id: str | None = None) -> list[str]:
        """Unique turn ids in transcript order (first appearance wins)."""
        seen: list[str] = []
        found: set[str] = set()
        for rec in self.all_records(session_id=session_id):
            tid = str(rec.get("turn_id") or "").strip()
            if not tid or tid in found:
                continue
            found.add(tid)
            seen.append(tid)
        return seen

    def turn_ids_before(
        self, before_turn_id: str | None, *, session_id: str | None = None
    ) -> set[str] | None:
        """Turn ids strictly before ``before_turn_id`` (point-in-time filter).

        Returns None when no filter applies (before_turn_id is None).
        If the cutoff turn is unknown, returns all known turn ids (cannot
        prove anything is after an unseen id).
        """
        if before_turn_id is None:
            return None
        order = self.ordered_turn_ids(session_id=session_id)
        if before_turn_id not in order:
            return set(order)
        idx = order.index(before_turn_id)
        return set(order[:idx])

    def recent_messages_before(
        self,
        before_turn_id: str | None,
        *,
        limit: int | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, str]]:
        """L0 window restricted to turns strictly before ``before_turn_id``."""
        allowed = self.turn_ids_before(before_turn_id, session_id=session_id)
        window = limit if limit is not None else self.recent_limit
        if allowed is None:
            return self.recent_messages(limit=window, session_id=session_id)
        messages: list[dict[str, str]] = []
        for rec in self.all_records(session_id=session_id):
            tid = str(rec.get("turn_id") or "")
            if tid and tid not in allowed:
                continue
            role = str(rec.get("role") or "")
            content = str(rec.get("content") or "")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages[-window:] if window else messages
