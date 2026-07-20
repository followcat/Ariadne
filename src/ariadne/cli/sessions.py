"""Session listing / resume helpers for the CLI and web host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    turns: int
    path: Path
    mtime: float
    preview: str = ""


def session_path(data_dir: Path, session_id: str) -> Path:
    return data_dir / "sessions" / f"{session_id}.jsonl"


def list_sessions(data_dir: Path) -> list[SessionInfo]:
    """Sessions recorded under data_dir/sessions/*.jsonl, newest first."""
    root = data_dir / "sessions"
    if not root.is_dir():
        return []
    out: list[SessionInfo] = []
    for path in root.glob("*.jsonl"):
        turns = 0
        preview = ""
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
                        if not preview:
                            preview = str(rec.get("content") or "").strip().replace("\n", " ")
                            if len(preview) > 80:
                                preview = preview[:77] + "…"
        except OSError:
            continue
        out.append(
            SessionInfo(
                session_id=path.stem,
                turns=turns,
                path=path,
                mtime=path.stat().st_mtime,
                preview=preview,
            )
        )
    out.sort(key=lambda s: -s.mtime)
    return out


def most_recent(data_dir: Path) -> str | None:
    sessions = list_sessions(data_dir)
    return sessions[0].session_id if sessions else None


def load_session_messages(
    data_dir: Path, session_id: str, *, limit: int = 200
) -> list[dict[str, str]]:
    """User/assistant messages for UI history (oldest first within the window)."""
    path = session_path(data_dir, session_id)
    if not path.is_file():
        return []
    from ..memory.transcript import TranscriptStore

    return TranscriptStore(path=path).recent_messages(limit=limit)


def delete_session(data_dir: Path, session_id: str) -> bool:
    """Remove transcript jsonl. Returns True if a file was deleted."""
    path = session_path(data_dir, session_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


def session_exists(data_dir: Path, session_id: str) -> bool:
    return session_path(data_dir, session_id).is_file()
