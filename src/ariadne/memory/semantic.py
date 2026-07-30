from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .embeddings import EmbeddingProvider, HashEmbeddingProvider, cosine

# ASCII words; CJK as single chars so short Chinese queries match longer lines.
_ASCII = re.compile(r"[a-z0-9_]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    raw = (text or "").lower()
    toks = _ASCII.findall(raw)
    toks.extend(_CJK.findall(raw))
    return toks


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
    embedding_model_id: str = "hash"

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                json.dumps({"chunks": [], "meta": {"seq": 0}}) + "\n", encoding="utf-8"
            )
        if self.embedder is None:
            self.embedder = HashEmbeddingProvider()
        model = getattr(self.embedder, "model", None) or getattr(
            self.embedder, "dims", None
        )
        if model is not None and self.embedding_model_id == "hash":
            if hasattr(self.embedder, "model"):
                self.embedding_model_id = f"openai:{self.embedder.model}"
            else:
                self.embedding_model_id = f"hash:{model}"

    def _read(self) -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"chunks": [], "meta": {"seq": 0}}
        data.setdefault("chunks", [])
        data.setdefault("meta", {"seq": 0})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _next_seq(self, data: dict[str, Any]) -> int:
        meta = data.setdefault("meta", {})
        seq = int(meta.get("seq") or 0) + 1
        meta["seq"] = seq
        return seq

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
        workspace_key: str = "",
        ts: float | None = None,
    ) -> None:
        data = self._read()
        chunks = data.setdefault("chunks", [])
        chunks[:] = [
            c
            for c in chunks
            if not (c.get("session_id") == session_id and c.get("turn_id") == turn_id)
        ]
        clock_ts = float(ts if ts is not None else time.time())
        clock_seq = self._next_seq(data)
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
                    "embedding_model": None,
                    "ts": clock_ts,
                    "seq": clock_seq,
                    "workspace_key": workspace_key or "",
                }
            )
        self._write(data)

    def lookup_turn_clock(
        self, *, turn_id: str, session_id: str | None = None
    ) -> tuple[float, int] | None:
        """Return (ts, seq) for a turn if indexed; prefer matching session_id."""
        tid = (turn_id or "").strip()
        if not tid:
            return None
        preferred: tuple[float, int] | None = None
        fallback: tuple[float, int] | None = None
        for chunk in self._read().get("chunks") or []:
            if str(chunk.get("turn_id") or "") != tid:
                continue
            ts = chunk.get("ts")
            if ts is None:
                continue
            clock = (float(ts), int(chunk.get("seq") or 0))
            if session_id is not None and str(chunk.get("session_id") or "") == session_id:
                preferred = clock
                break
            if fallback is None:
                fallback = clock
        return preferred if preferred is not None else fallback

    async def ensure_embeddings(self, *, session_id: str | None = None, limit: int = 200) -> int:
        data = self._read()
        pending = []
        model_id = self.embedding_model_id
        for i, chunk in enumerate(data.get("chunks") or []):
            if session_id and chunk.get("session_id") != session_id:
                continue
            # Re-embed when model stamp mismatches (model change invalidation).
            if chunk.get("embedding") and chunk.get("embedding_model") == model_id:
                continue
            pending.append((i, str(chunk.get("text") or "")))
            if len(pending) >= limit:
                break
        if not pending or self.embedder is None:
            return 0
        vectors = await self.embedder.embed([t for _, t in pending])
        for (idx, _), vec in zip(pending, vectors):
            data["chunks"][idx]["embedding"] = vec
            data["chunks"][idx]["embedding_model"] = model_id
        self._write(data)
        return len(pending)

    @staticmethod
    def demote_multiplier(
        chunk: dict[str, Any],
        *,
        demote_entity_ids: set[str] | None = None,
        authoritative_fields: dict[str, dict[str, Any]] | None = None,
    ) -> float:
        """Field-level stale-trap demotion (MEMORY design L4)."""
        ents = {str(e) for e in (chunk.get("entity_ids") or [])}
        if not ents:
            return 1.0
        text = str(chunk.get("text") or "").lower()
        mult = 1.0
        if authoritative_fields:
            for eid in ents:
                fields = authoritative_fields.get(eid)
                if not fields:
                    continue
                current_vals: list[str] = []
                for payload in fields.values():
                    if isinstance(payload, dict):
                        val = payload.get("value")
                    else:
                        val = payload
                    if val is None:
                        continue
                    s = str(val).strip().lower()
                    if len(s) >= 2:
                        current_vals.append(s)
                if not current_vals:
                    continue
                if any(v in text for v in current_vals):
                    continue
                mult *= 0.2
            return mult
        if demote_entity_ids and ents & demote_entity_ids:
            return 0.2
        return 1.0

    @staticmethod
    def _chunk_passes_filters(
        chunk: dict[str, Any],
        *,
        session_id: str | None,
        allowed_turn_ids: set[str] | None,
        before_ts: float | None,
    ) -> bool:
        if session_id is not None and chunk.get("session_id") != session_id:
            return False
        tid = str(chunk.get("turn_id") or "")
        if allowed_turn_ids is not None and tid and tid not in allowed_turn_ids:
            return False
        if before_ts is not None:
            ts = chunk.get("ts")
            if ts is None:
                # Missing clock under as-of filter: exclude (honest migration).
                return False
            if float(ts) >= float(before_ts):
                return False
        return True

    def search(
        self,
        *,
        session_id: str | None,
        query: str,
        limit: int = 5,
        expand_aliases: list[str] | None = None,
        demote_entity_ids: set[str] | None = None,
        authoritative_fields: dict[str, dict[str, Any]] | None = None,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Lexical search. ``session_id=None`` searches all sessions in this index."""
        q = query or ""
        if expand_aliases:
            q = q + " " + " ".join(expand_aliases)
        qv_bow = _vector(q)
        data = self._read()
        scored: list[tuple[float, dict[str, Any]]] = []
        for chunk in data.get("chunks") or []:
            if not self._chunk_passes_filters(
                chunk,
                session_id=session_id,
                allowed_turn_ids=allowed_turn_ids,
                before_ts=before_ts,
            ):
                continue
            text = str(chunk.get("text") or "")
            score = _cosine_bow(qv_bow, _vector(text))
            # Substring boost helps short Chinese / exact needles.
            if q.strip() and q.strip().lower() in text.lower():
                score = max(score, 0.55)
            score *= self.demote_multiplier(
                chunk,
                demote_entity_ids=demote_entity_ids,
                authoritative_fields=authoritative_fields,
            )
            if score > 0:
                scored.append((score, chunk))
        return self._pack_hits(scored, limit=limit)

    async def search_hybrid(
        self,
        *,
        session_id: str | None,
        query: str,
        limit: int = 5,
        expand_aliases: list[str] | None = None,
        demote_entity_ids: set[str] | None = None,
        authoritative_fields: dict[str, dict[str, Any]] | None = None,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid lexical+embedding. ``session_id=None`` = whole index."""
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
            if not self._chunk_passes_filters(
                chunk,
                session_id=session_id,
                allowed_turn_ids=allowed_turn_ids,
                before_ts=before_ts,
            ):
                continue
            text = str(chunk.get("text") or "")
            bow = _cosine_bow(q_bow, _vector(text))
            emb_score = 0.0
            if chunk.get("embedding"):
                emb_score = cosine(q_emb, list(map(float, chunk["embedding"])))
            score = 0.45 * bow + 0.55 * emb_score
            if q.strip() and q.strip().lower() in text.lower():
                score = max(score, 0.55)
            score *= self.demote_multiplier(
                chunk,
                demote_entity_ids=demote_entity_ids,
                authoritative_fields=authoritative_fields,
            )
            if score > 0:
                scored.append((score, chunk))
        return self._pack_hits(scored, limit=limit)

    def _pack_hits(
        self, scored: list[tuple[float, dict[str, Any]]], *, limit: int
    ) -> list[dict[str, Any]]:
        scored.sort(key=lambda item: -item[0])
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for score, chunk in scored:
            tid = str(chunk.get("turn_id") or "")
            sid = str(chunk.get("session_id") or "")
            if not tid:
                continue
            key = f"{sid}:{tid}"
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "turn_id": tid,
                    "session_id": sid,
                    "kind": chunk.get("kind"),
                    "score": round(score, 4),
                    "text": str(chunk.get("text") or "")[:400],
                    "snippet": str(chunk.get("text") or "")[:400],
                    "ts": chunk.get("ts"),
                    "seq": chunk.get("seq"),
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
