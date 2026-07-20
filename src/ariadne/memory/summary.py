"""L1 turn summaries with async-ready status machine."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from .json_file import locked_read_json, locked_update_json, locked_write_json

SummaryStatus = Literal["pending", "processing", "ready", "failed", "not_applicable"]
CompressorFn = Callable[[str], str]

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")


def grounded_compress(source_text: str, *, max_chars: int = 400) -> str:
    """Grounded multi-sentence compressor (no free invention).

    Picks leading + trailing sentences and high-signal middle lines that
    already appear in the source (bullet lines, quoted values, key:value).
    Falls back to head truncate when structure is flat.
    """
    src = (source_text or "").strip()
    if not src:
        return ""
    if len(src) <= max_chars:
        return src
    sentences = [s.strip() for s in _SENT_SPLIT.split(src) if s and s.strip()]
    if len(sentences) <= 1:
        return src[:max_chars]
    picked: list[str] = []
    used: set[str] = set()

    def add(s: str) -> None:
        key = s[:80]
        if key in used:
            return
        used.add(key)
        picked.append(s)

    add(sentences[0])
    mid = sentences[1:-1]
    signal = [
        s
        for s in mid
        if re.search(r"(^[-*]|\d|[/\\:=]|`|\b[A-Z]{2,}\b)", s)
    ]
    for s in signal[:3]:
        add(s)
    if sentences[-1] != sentences[0]:
        add(sentences[-1])
    out = " ".join(picked)
    if len(out) > max_chars:
        head = max_chars // 2 - 10
        tail = max_chars - head - 5
        out = out[:head] + " … " + out[-tail:]
    if not out:
        return src[:max_chars]
    return out


def _compressor_kind(fn: CompressorFn | None) -> str:
    if fn is None:
        return "grounded_compress"
    kind = getattr(fn, "kind", None)
    if kind:
        return str(kind)
    name = getattr(fn, "__name__", "") or ""
    if "grounded" in name:
        return "grounded_compress"
    return "custom"


@dataclass
class TurnSummaryStore:
    """L1 store: enqueue → process → ready (design: no invent; raw fallback when missing).

    JSON path is fcntl-locked so agent turns and sub-process workers can share
    the same ``summaries.json`` without lost updates.
    """

    path: Path
    compressor: CompressorFn | None = None
    max_summary_chars: int = 400

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, {})
        if self.compressor is None:
            fn = lambda t: grounded_compress(t, max_chars=self.max_summary_chars)  # noqa: E731
            fn.kind = "grounded_compress"  # type: ignore[attr-defined]
            self.compressor = fn

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default={})
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        locked_write_json(self.path, data)

    def put(
        self,
        *,
        session_id: str,
        turn_id: str,
        summary_text: str,
        status: SummaryStatus = "ready",
    ) -> None:
        """Write a summary entry (legacy ready path + status field)."""

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            sess = data.setdefault(session_id, {})
            sess[turn_id] = {
                "status": status,
                "summary_text": summary_text,
                "updated_at": time.time(),
            }
            return data

        locked_update_json(self.path, mut, default={})

    def enqueue(
        self,
        *,
        session_id: str,
        turn_id: str,
        source_text: str,
    ) -> None:
        """Mark summary pending with source text for later compression."""

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            sess = data.setdefault(session_id, {})
            existing = sess.get(turn_id) or {}
            if existing.get("status") == "ready" and existing.get("summary_text"):
                return data
            sess[turn_id] = {
                "status": "pending",
                "source_text": (source_text or "")[:4000],
                "summary_text": "",
                "updated_at": time.time(),
            }
            return data

        locked_update_json(self.path, mut, default={})

    def process_pending(
        self, *, session_id: str | None = None, max_jobs: int = 32
    ) -> int:
        """Compress pending entries to ready.

        Compressors may call LLM (seconds). Claim under lock, compress
        **outside** the lock, then finish under lock so other processes are not
        blocked on model latency.
        """
        compress = self.compressor or (
            lambda t: grounded_compress(t, max_chars=self.max_summary_chars)
        )
        kind = _compressor_kind(compress)
        n = 0
        while n < max_jobs:
            claimed: dict[str, str] | None = None

            def claim(data: dict[str, Any]) -> dict[str, Any]:
                nonlocal claimed
                for sid, sess in list(data.items()):
                    if session_id is not None and sid != session_id:
                        continue
                    if not isinstance(sess, dict):
                        continue
                    for tid, payload in list(sess.items()):
                        if not isinstance(payload, dict):
                            continue
                        if payload.get("status") != "pending":
                            continue
                        src = str(payload.get("source_text") or "").strip()
                        payload["status"] = "processing"
                        payload["updated_at"] = time.time()
                        claimed = {"session_id": sid, "turn_id": tid, "source_text": src}
                        return data
                return data

            locked_update_json(self.path, claim, default={})
            if claimed is None:
                break

            src = claimed["source_text"]
            if not src:
                summary_text = ""
                status: SummaryStatus = "not_applicable"
                comp_label = kind
            else:
                summary_text = compress(src)
                status = "ready"
                # If llm fell back, text may still look grounded; label as requested kind.
                comp_label = kind

            sid = claimed["session_id"]
            tid = claimed["turn_id"]

            def finish(data: dict[str, Any]) -> dict[str, Any]:
                sess = data.get(sid)
                if not isinstance(sess, dict):
                    return data
                payload = sess.get(tid)
                if not isinstance(payload, dict):
                    return data
                # Only finish if we still own the processing claim.
                if payload.get("status") != "processing":
                    return data
                payload["status"] = status
                payload["summary_text"] = summary_text
                payload["compressor"] = comp_label
                payload["updated_at"] = time.time()
                return data

            locked_update_json(self.path, finish, default={})
            n += 1
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
                if isinstance(p, dict) and p.get("status") in {"pending", "processing"}
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
        seen: set[str] = set()
        lines = ["[HISTORICAL_CONTEXT: MAY BE SUPERSEDED BY CONVERSATION_STATE]"]
        for item in items:
            tid = item["turn_id"]
            if tid in seen:
                continue
            seen.add(tid)
            lines.append(f"- turn {tid}: {item['summary_text']}")
        return "\n".join(lines)
