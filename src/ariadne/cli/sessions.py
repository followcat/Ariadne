"""Session listing / resume helpers for the CLI and web host."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    turns: int
    path: Path
    mtime: float
    preview: str = ""
    title: str = ""
    title_source: Literal["auto", "user", ""] = ""


def session_path(data_dir: Path, session_id: str) -> Path:
    return data_dir / "sessions" / f"{session_id}.jsonl"


def session_meta_path(data_dir: Path, session_id: str) -> Path:
    return data_dir / "sessions" / "meta" / f"{session_id}.json"


def session_exists(data_dir: Path, session_id: str) -> bool:
    return session_path(data_dir, session_id).is_file()


def _read_meta(data_dir: Path, session_id: str) -> dict[str, Any]:
    path = session_meta_path(data_dir, session_id)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_meta(data_dir: Path, session_id: str, meta: dict[str, Any]) -> None:
    path = session_meta_path(data_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_session_title(data_dir: Path, session_id: str) -> tuple[str, str]:
    """Return (title, source) where source is auto|user|''."""
    meta = _read_meta(data_dir, session_id)
    title = str(meta.get("title") or "").strip()
    source = str(meta.get("source") or "").strip()
    if source not in {"auto", "user"}:
        source = "user" if title else ""
    return title, source


def set_session_title(
    data_dir: Path,
    session_id: str,
    title: str,
    *,
    source: Literal["auto", "user"] = "user",
) -> dict[str, Any]:
    cleaned = " ".join(str(title or "").split()).strip()
    if not cleaned:
        raise ValueError("title must be non-empty")
    if len(cleaned) > 80:
        cleaned = cleaned[:77] + "…"
    meta = {
        "title": cleaned,
        "source": source,
        "updated_at": time.time(),
    }
    _write_meta(data_dir, session_id, meta)
    return meta


def clear_session_title(data_dir: Path, session_id: str) -> bool:
    path = session_meta_path(data_dir, session_id)
    if not path.is_file():
        return False
    path.unlink()
    return True


_IMAGE_PLACEHOLDER = re.compile(r"\[image[^\]]*\]", re.I)
_NOISE_PREFIX = re.compile(
    r"^(请|帮我|帮忙|麻烦|能否|可以|怎么|如何|what|how|please|pls|hey|hi|hello)[\s,，：:]*",
    re.I,
)


def suggest_title_from_messages(
    messages: list[dict[str, str]], *, max_len: int = 40
) -> str:
    """Heuristic topic title from early user (and optional assistant) turns — no LLM."""
    users: list[str] = []
    for m in messages:
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "")
        text = _IMAGE_PLACEHOLDER.sub(" ", text)
        text = text.replace("\n", " ").strip()
        if text:
            users.append(text)
        if len(users) >= 2:
            break
    if not users:
        return "新对话"
    primary = users[0]
    primary = _NOISE_PREFIX.sub("", primary).strip() or users[0]
    # Prefer first sentence / clause
    for sep in ("。", "！", "？", ".", "!", "?", "\n"):
        if sep in primary:
            primary = primary.split(sep, 1)[0].strip()
            break
    primary = re.sub(r"\s+", " ", primary).strip(" -—|·")
    if not primary:
        primary = users[0][:max_len]
    if len(primary) > max_len:
        primary = primary[: max_len - 1].rstrip() + "…"
    return primary


def refresh_session_title(
    data_dir: Path,
    session_id: str,
    *,
    force: bool = False,
    max_len: int = 40,
) -> dict[str, Any] | None:
    """Auto-summarize title from transcript.

    Skips when source is user unless force=True. Returns meta or None if empty.
    """
    title, source = get_session_title(data_dir, session_id)
    if source == "user" and not force:
        return {"title": title, "source": source, "skipped": True}
    messages = load_session_messages(data_dir, session_id, limit=12)
    if not messages:
        return None
    suggested = suggest_title_from_messages(messages, max_len=max_len)
    return set_session_title(data_dir, session_id, suggested, source="auto")


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
            # Fast path for large transcripts: sample head for preview, count via
            # lightweight line scan without full JSON parse of every assistant line.
            size = path.stat().st_size
            with path.open(encoding="utf-8") as fh:
                if size > 512_000:
                    # Preview from first 64KB; turn count ≈ count of "role":"user" markers
                    head = fh.read(65_536)
                    for line in head.splitlines():
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
                                preview = _IMAGE_PLACEHOLDER.sub(" ", preview).strip()
                                if len(preview) > 80:
                                    preview = preview[:77] + "…"
                    # Approximate remaining user turns from marker count in the rest
                    rest = fh.read()
                    turns += rest.count('"role": "user"') + rest.count('"role":"user"')
                    # head already counted exact user rows; rest may overcount slightly — ok for UI
                else:
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
                                preview = _IMAGE_PLACEHOLDER.sub(" ", preview).strip()
                                if len(preview) > 80:
                                    preview = preview[:77] + "…"
        except OSError:
            continue
        title, source = get_session_title(data_dir, path.stem)
        if not title:
            title = preview[:40] + ("…" if len(preview) > 40 else "") if preview else path.stem
        out.append(
            SessionInfo(
                session_id=path.stem,
                turns=turns,
                path=path,
                mtime=path.stat().st_mtime,
                preview=preview,
                title=title,
                title_source=source,  # type: ignore[arg-type]
            )
        )
    out.sort(key=lambda s: -s.mtime)
    return out


def most_recent(data_dir: Path) -> str | None:
    sessions = list_sessions(data_dir)
    return sessions[0].session_id if sessions else None


def load_session_messages(
    data_dir: Path, session_id: str, *, limit: int = 80
) -> list[dict[str, Any]]:
    """User/assistant messages for UI history (oldest first within the window).

    Each message may include ``turn_id`` when the transcript was stamped.
    Default limit is modest for snappy chat-switch UX; callers can raise it.
    """
    path = session_path(data_dir, session_id)
    if not path.is_file():
        return []
    from ..memory.transcript import TranscriptStore

    return TranscriptStore(path=path).recent_messages(limit=limit)


def annotate_turn_indices(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add 1-based ``turn_index`` for each user→assistant pair (display badges)."""
    idx = 0
    out: list[dict[str, Any]] = []
    for m in messages:
        row = dict(m)
        if row.get("role") == "user":
            idx += 1
            row["turn_index"] = idx
        elif row.get("role") == "assistant":
            row["turn_index"] = idx if idx > 0 else 1
        out.append(row)
    return out


def delete_session(data_dir: Path, session_id: str) -> bool:
    """Remove transcript jsonl, turn tools history, and title meta."""
    path = session_path(data_dir, session_id)
    meta = session_meta_path(data_dir, session_id)
    turns = data_dir / "sessions" / f"{session_id}.turns.jsonl"
    deleted = False
    if path.is_file():
        path.unlink()
        deleted = True
    if meta.is_file():
        meta.unlink()
        deleted = True
    if turns.is_file():
        turns.unlink()
        deleted = True
    return deleted
