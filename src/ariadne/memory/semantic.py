from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingProvider, HashEmbeddingProvider, cosine

_TOKEN = re.compile(r"[a-z0-9_]{2,}")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _vector(text: str) -> Counter[str]:
    return Counter(_tokenize(text))


def _cosine_bow(a: Counter[str], b: Counter[str]) -> float:
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
    path: Path
    embedder: EmbeddingProvider | None = None

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(json.dumps({"chunks": []}) + "\n", encoding="utf-8")
        if self.embedder is None:
            self.embedder = HashEmbeddingProvider()

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
        summary_text: str = "",
        entity_ids: list[str] | None = None,
    ) -> None:
        data = self._read()
        chunks = data.setdefault("chunks", [])
        chunks[:] = [
            c
            for c in chunks
            if not (c.get("session_id") == session_id and c.get("turn_id") == turn_id)
        ]
        for kind, text in (
            ("user", user_text),
            ("assistant", assistant_text),
            ("tool", tool_text),
            ("summary", summary_text),
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
                    "entity_ids": list(entity_ids or []),
                    "embedding": None,
                }
            )
        self._write(data)

    async def ensure_embeddings(self, *, session_id: str | None = None, limit: int = 200) -> int:
        data = self._read()
        pending = []
        for i, chunk in enumerate(data.get("chunks") or []):
            if session_id and chunk.get("session_id") != session_id:
                continue
            if chunk.get("embedding"):
                continue
            pending.append((i, str(chunk.get("text") or "")))
            if len(pending) >= limit:
                break
        if not pending or self.embedder is None:
            return 0
        vectors = await self.embedder.embed([t for _, t in pending])
        for (idx, _), vec in zip(pending, vectors):
            data["chunks"][idx]["embedding"] = vec
        self._write(data)
        return len(pending)

    def search(
        self,
        *,
        session_id: str,
        query: str,
        limit: int = 5,
        expand_aliases: list[str] | None = None,
        demote_entity_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        q = query or ""
        if expand_aliases:
            q = q + " " + " ".join(expand_aliases)
        qv_bow = _vector(q)
        data = self._read()
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in data.get("chunks") or []:
            if chunk.get("session_id") != session_id:
                continue
            text = str(chunk.get("text") or "")
            score = _cosine_bow(qv_bow, _vector(text))
            emb = chunk.get("embedding")
            # hybrid: if embeddings present on chunk, blend later via optional query emb sync path
            if demote_entity_ids:
                ents = set(chunk.get("entity_ids") or [])
                if ents & demote_entity_ids:
                    score *= 0.2
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: -item[0])
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, chunk in scored:
            tid = str(chunk.get("turn_id"))
            if tid in seen:
                continue
            seen.add(tid)
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

    async def search_hybrid(
        self,
        *,
        session_id: str,
        query: str,
        limit: int = 5,
        expand_aliases: list[str] | None = None,
        demote_entity_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        await self.ensure_embeddings(session_id=session_id)
        q = query or ""
        if expand_aliases:
            q = q + " " + " ".join(expand_aliases)
        assert self.embedder is not None
        q_emb = (await self.embedder.embed([q]))[0]
        data = self._read()
        scored: list[tuple[float, dict[str, Any]]] = []
        q_bow = _vector(q)
        for chunk in data.get("chunks") or []:
            if chunk.get("session_id") != session_id:
                continue
            text = str(chunk.get("text") or "")
            bow = _cosine_bow(q_bow, _vector(text))
            emb_score = 0.0
            if chunk.get("embedding"):
                emb_score = cosine(q_emb, list(map(float, chunk["embedding"])))
            score = 0.45 * bow + 0.55 * emb_score
            if demote_entity_ids:
                ents = set(chunk.get("entity_ids") or [])
                if ents & demote_entity_ids:
                    score *= 0.2
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: -item[0])
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, chunk in scored:
            tid = str(chunk.get("turn_id"))
            if tid in seen:
                continue
            seen.add(tid)
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
            lines.append(
                f"- turn {hit['turn_id']} ({hit.get('kind')}, score={hit.get('score')}): {hit.get('text')}"
            )
        return "\n".join(lines)
