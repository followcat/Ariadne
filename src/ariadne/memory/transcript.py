from __future__ import annotations

import json
from dataclasses import dataclass, field
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

    def recent_messages(self) -> list[dict[str, str]]:
        lines = [ln for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        selected = lines[-self.recent_limit :]
        messages: list[dict[str, str]] = []
        for line in selected:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = str(item.get("role") or "")
            content = str(item.get("content") or "")
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
        return messages
