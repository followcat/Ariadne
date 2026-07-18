"""Session listing / resume helpers for the CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    turns: int
    path: Path
    mtime: float


def list_sessions(data_dir: Path) -> list[SessionInfo]:
    """Sessions recorded under data_dir/sessions/*.jsonl, newest first."""
    root = data_dir / "sessions"
    if not root.is_dir():
        return []
    out: list[SessionInfo] = []
    for path in root.glob("*.jsonl"):
        turns = 0
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("role") == "user":
                        turns += 1
        except OSError:
            continue
        out.append(
            SessionInfo(
                session_id=path.stem,
                turns=turns,
                path=path,
                mtime=path.stat().st_mtime,
            )
        )
    out.sort(key=lambda s: -s.mtime)
    return out


def most_recent(data_dir: Path) -> str | None:
    sessions = list_sessions(data_dir)
    return sessions[0].session_id if sessions else None
