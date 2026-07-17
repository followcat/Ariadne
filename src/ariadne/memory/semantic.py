from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_TOKEN = re.compile(r"[a-z0-9_]{2,}")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _vector(text: str) -> Counter[str]:
    return Counter(_tokenize(text))


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


@dataclass
class SemanticIndex:
    """L4 lexical multi-chunk index (no external embed dependency)."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"chunks": []}) + "\n", encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def index_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        tool_text: str = "",
    ) -> None:
        data = self._read()
        chunks = data.setdefault("chunks", [])
        # drop old chunks for same turn
        chunks[:] = [c for c in chunks if not (c.get("session_id") == session_id and c.get("turn_id") == turn_id)]
        for kind, text in (
            ("user", user_text),
            ("assistant", assistant_text),
            ("tool", tool_text),
        ):
            clean = (text or "").strip()
            if not clean:
                continue
            chunks.append(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "kind": kind,
                    "text": clean[:4000],
                }
            )
        self._write(data)

    def search(self, *, session_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        qv = _vector(query)
        data = self._read()
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in data.get("chunks") or []:
            if chunk.get("session_id") != session_id:
                continue
            score = _cosine(qv, _vector(str(chunk.get("text") or "")))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: -item[0])
        out = []
        seen_turns: set[str] = set()
        for score, chunk in scored:
            tid = str(chunk.get("turn_id"))
            if tid in seen_turns:
                continue
            seen_turns.add(tid)
            out.append(
                {
                    "turn_id": tid,
                    "kind": chunk.get("kind"),
                    "score": round(score, 4),
                    "text": str(chunk.get("text") or "")[:400],
                }
            )
            if len(out) >= limit:
                break
        return out

    def render(self, hits: list[dict[str, Any]]) -> str:
        if not hits:
            return ""
        lines = ["[SEMANTIC_HITS: RETRIEVAL ONLY]"]
        for hit in hits:
            lines.append(f"- turn {hit['turn_id']} ({hit.get('kind')}, score={hit.get('score')}): {hit.get('text')}")
        return "\n".join(lines)
