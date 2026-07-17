from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class TurnSummaryStore:
    """L1 async-ready store. Personal v1 can write summaries inline after a turn."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}\n", encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def put(self, *, session_id: str, turn_id: str, summary_text: str) -> None:
        data = self._read()
        sess = data.setdefault(session_id, {})
        sess[turn_id] = {"status": "ready", "summary_text": summary_text}
        self._write(data)

    def list_ready(self, session_id: str, *, limit: int = 8) -> list[dict[str, str]]:
        data = self._read()
        sess = data.get(session_id) or {}
        items = []
        for turn_id, payload in sess.items():
            if payload.get("status") == "ready" and payload.get("summary_text"):
                items.append({"turn_id": turn_id, "summary_text": str(payload["summary_text"])})
        return items[-limit:]

    def render(self, session_id: str, *, limit: int = 8) -> str:
        items = self.list_ready(session_id, limit=limit)
        if not items:
            return ""
        lines = ["[HISTORICAL_CONTEXT: MAY BE SUPERSEDED BY CONVERSATION_STATE]"]
        for item in items:
            lines.append(f"- turn {item['turn_id']}: {item['summary_text']}")
        return "\n".join(lines)
