"""Persist per-turn tool call records for web UI (survives process restart).

Each session has a sibling JSONL: ``{session_id}.turns.jsonl`` next to the
transcript. One row per completed/failed turn with tool traces + light stats.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def turns_path(sessions_dir: Path, session_id: str) -> Path:
    """Path for turn tool history next to the session transcript JSONL.

    * Web account: ``{user_data}/sessions/{session_id}.turns.jsonl``
    * Atelier: ``{project}/.ariadne/sessions/{session_id}.turns.jsonl``
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / f"{sid}.turns.jsonl"


def resolve_sessions_dir(data_dir: Path) -> Path:
    """Account data_dir → sessions/; atelier project.sessions_dir pass-through."""
    # Already a sessions dir (contains *.jsonl transcripts or ends with sessions)
    if data_dir.name == "sessions":
        return data_dir
    return data_dir / "sessions"


def _safe_jsonable(obj: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return str(obj)[:500]
    if obj is None or isinstance(obj, (bool, int, float, str)):
        if isinstance(obj, str) and len(obj) > 12_000:
            return obj[:12_000] + "…[truncated]"
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i > 80:
                out["…"] = f"+{len(obj) - 80} keys"
                break
            out[str(k)[:120]] = _safe_jsonable(v, depth=depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        items = list(obj)
        clipped = [_safe_jsonable(x, depth=depth + 1) for x in items[:40]]
        if len(items) > 40:
            clipped.append(f"…+{len(items) - 40} items")
        return clipped
    if hasattr(obj, "__dataclass_fields__"):
        return {
            k: _safe_jsonable(getattr(obj, k), depth=depth + 1)
            for k in obj.__dataclass_fields__  # type: ignore[attr-defined]
        }
    return str(obj)[:2000]


def tool_calls_from_result_payload(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize tool_calls list from render_json(TurnResult) payload."""
    if not result or not isinstance(result, dict):
        return []
    raw = result.get("tool_calls") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "call_id": str(t.get("call_id") or t.get("id") or ""),
                "name": str(t.get("name") or "?"),
                "status": str(t.get("status") or "completed"),
                "arguments": _safe_jsonable(t.get("arguments")),
                "output": _safe_jsonable(t.get("output")),
                "error": _safe_jsonable(t.get("error")),
            }
        )
    return out


def append_turn_record(
    sessions_dir: Path,
    session_id: str,
    *,
    turn_id: str,
    status: str = "completed",
    tools: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    model: str = "",
    text: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one turn record. Idempotent-ish: skip if same turn_id already last row."""
    tid = (turn_id or "").strip()
    if not tid:
        tid = f"t-{int(time.time() * 1000)}"
    path = turns_path(sessions_dir, session_id)
    # Skip duplicate consecutive same turn_id (SSE retry)
    try:
        if path.is_file() and path.stat().st_size > 0:
            with path.open("rb") as fh:
                fh.seek(max(0, path.stat().st_size - 4000))
                tail = fh.read().decode("utf-8", errors="replace")
            last = ""
            for line in reversed(tail.splitlines()):
                if line.strip():
                    last = line
                    break
            if last:
                prev = json.loads(last)
                if str(prev.get("turn_id") or "") == tid:
                    return prev
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    existing = list_turn_records(sessions_dir, session_id)
    turn_index = len(existing) + 1
    row: dict[str, Any] = {
        "turn_id": tid,
        "turn_index": turn_index,
        "session_id": session_id,
        "status": status or "completed",
        "ts": time.time(),
        "model": model or "",
        "usage": usage or {},
        "tools": tools or [],
        "tool_count": len(tools or []),
        "text_preview": (text or "")[:240],
    }
    if extra:
        for k, v in extra.items():
            if k not in row:
                row[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return row


def append_turn_from_result_payload(
    sessions_dir: Path,
    session_id: str,
    result: dict[str, Any] | None,
    *,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Persist from web SSE /api turn result dict (render_json shape)."""
    if not result or not isinstance(result, dict):
        return None
    tid = str(result.get("turn_id") or "").strip()
    if not tid:
        return None
    tools = tool_calls_from_result_payload(result)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return append_turn_record(
        sessions_dir,
        session_id,
        turn_id=tid,
        status=status or str(result.get("status") or "completed"),
        tools=tools,
        usage=usage,
        model=str(result.get("model") or ""),
        text=str(result.get("text") or ""),
    )


def list_turn_records(
    sessions_dir: Path, session_id: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    path = turns_path(sessions_dir, session_id)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("turn_id"):
            rows.append(rec)
    # Re-number turn_index for display stability
    for i, r in enumerate(rows, start=1):
        r["turn_index"] = i
    return rows[-limit:] if limit else rows


def get_turn_record(
    sessions_dir: Path, session_id: str, turn_id: str
) -> dict[str, Any] | None:
    tid = (turn_id or "").strip()
    if not tid:
        return None
    for rec in reversed(list_turn_records(sessions_dir, session_id, limit=500)):
        if str(rec.get("turn_id") or "") == tid:
            return rec
    return None
