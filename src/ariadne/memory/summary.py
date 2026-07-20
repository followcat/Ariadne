"""L1 turn summaries with async-ready status machine."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SummaryStatus = Literal["pending", "ready", "failed", "not_applicable"]


@dataclass
class TurnSummaryStore:
    """L1 store: enqueue → process → ready (design: no invent; raw fallback when missing)."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}\n", encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def put(
        self,
        *,
        session_id: str,
        turn_id: str,
        summary_text: str,
        status: SummaryStatus = "ready",
    ) -> None:
        """Write a summary entry (legacy ready path + status field)."""
        data = self._read()
        sess = data.setdefault(session_id, {})
        sess[turn_id] = {
            "status": status,
            "summary_text": summary_text,
            "updated_at": time.time(),
        }
        self._write(data)

    def enqueue(
        self,
        *,
        session_id: str,
        turn_id: str,
        source_text: str,
    ) -> None:
        """Mark summary pending with source text for later compression."""
        data = self._read()
        sess = data.setdefault(session_id, {})
        existing = sess.get(turn_id) or {}
        if existing.get("status") == "ready" and existing.get("summary_text"):
            return
        sess[turn_id] = {
            "status": "pending",
            "source_text": (source_text or "")[:4000],
            "summary_text": "",
            "updated_at": time.time(),
        }
        self._write(data)

    def process_pending(
        self, *, session_id: str | None = None, max_jobs: int = 32
    ) -> int:
        """Compress pending entries to ready (stub compressor: grounded truncate).

        Never invents facts beyond the source_text. Returns number processed.
        """
        data = self._read()
        n = 0
        for sid, sess in list(data.items()):
            if session_id is not None and sid != session_id:
                continue
            if not isinstance(sess, dict):
                continue
            for turn_id, payload in list(sess.items()):
                if n >= max_jobs:
                    break
                if not isinstance(payload, dict):
                    continue
                if payload.get("status") != "pending":
                    continue
                src = str(payload.get("source_text") or "").strip()
                if not src:
                    payload["status"] = "not_applicable"
                    payload["summary_text"] = ""
                else:
                    # Grounded stub: first 400 chars of source (no free invention).
                    payload["summary_text"] = src[:400]
                    payload["status"] = "ready"
                payload["updated_at"] = time.time()
                n += 1
            if n >= max_jobs:
                break
        if n:
            self._write(data)
        return n

    def list_ready(
        self,
        session_id: str,
        *,
        limit: int = 8,
        allowed_turn_ids: set[str] | None = None,
    ) -> list[dict[str, str]]:
        data = self._read()
        sess = data.get(session_id) or {}
        items = []
        for turn_id, payload in sess.items():
            if not isinstance(payload, dict):
                continue
            if allowed_turn_ids is not None and turn_id not in allowed_turn_ids:
                continue
            if payload.get("status") == "ready" and payload.get("summary_text"):
                items.append({"turn_id": turn_id, "summary_text": str(payload["summary_text"])})
        return items[-limit:]

    def pending_count(self, session_id: str | None = None) -> int:
        data = self._read()
        n = 0
        for sid, sess in data.items():
            if session_id is not None and sid != session_id:
                continue
            if not isinstance(sess, dict):
                continue
            n += sum(
                1
                for p in sess.values()
                if isinstance(p, dict) and p.get("status") == "pending"
            )
        return n

    def render(
        self,
        session_id: str,
        *,
        limit: int = 8,
        allowed_turn_ids: set[str] | None = None,
        process_inline: bool = True,
    ) -> str:
        # Process pending inline so personal v1 stays usable without a worker.
        if process_inline:
            self.process_pending(session_id=session_id, max_jobs=limit * 2)
        items = self.list_ready(
            session_id, limit=limit, allowed_turn_ids=allowed_turn_ids
        )
        if not items:
            return ""
        lines = ["[HISTORICAL_CONTEXT: MAY BE SUPERSEDED BY CONVERSATION_STATE]"]
        for item in items:
            lines.append(f"- turn {item['turn_id']}: {item['summary_text']}")
        return "\n".join(lines)
