from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..types import LayerReport, MemoryContextSummary
from .curated import CuratedStore
from .embeddings import HashEmbeddingProvider
from .projection import ProjectionWorker
from .semantic import SemanticIndex
from .state import ConversationStateStore
from .summary import TurnSummaryStore
from .transcript import TranscriptStore

STATE_DELTA_MAX_MESSAGES = 6
STATE_DELTA_CHAR_CAP = 2000


@dataclass
class MemoryFacade:
    transcript: TranscriptStore
    curated: CuratedStore
    state: ConversationStateStore
    summaries: TurnSummaryStore
    semantic: SemanticIndex
    projection: ProjectionWorker | None = None
    recent_limit: int = 4
    hybrid_semantic: bool = True

    def build_context(self, *, session_id: str, query: str, user_id: str | None = None) -> tuple[str, MemoryContextSummary]:
        # sync wrapper for simple callers
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.build_context_async(session_id=session_id, user_id=user_id, query=query))
        # if already in loop, fall back to lexical path without await hybrid
        return self._build_context_sync(session_id=session_id, query=query)

    def get_curated(self, *, session_id: str) -> dict[str, object]:
        """Convenience read for hosts: user-scope + session-scope curated entries."""
        user = self.curated.apply(action="read", scope="user", session_id=session_id)
        session = self.curated.apply(action="read", scope="session", session_id=session_id)
        return {"user": user["entries"], "session": session["entries"]}

    def _state_delta(self, session_id: str) -> tuple[str, list[str]] | None:
        """last_good_plus_delta read mode: raw turns newer than the state watermark.

        Rendered state is always the last succeeded projection (last-good). Raw
        turns after the watermark are appended as a bounded delta block that
        takes precedence on conflict, so projection lag never blocks or lies.
        """
        watermark = self.state.watermark(session_id)
        if watermark is None:
            return None
        records = self.transcript.records_after(watermark)
        msgs = [
            r
            for r in records
            if r.get("role") in {"user", "assistant"} and str(r.get("content") or "").strip()
        ][-STATE_DELTA_MAX_MESSAGES:]
        if not msgs:
            return None
        kept: list[dict[str, object]] = []
        total = 0
        for m in reversed(msgs):
            content = str(m["content"])
            if kept and total + len(content) > STATE_DELTA_CHAR_CAP:
                break
            kept.append(m)
            total += len(content)
        kept.reverse()
        lines = ["[RECENT_TURNS: NEWER THAN CONVERSATION_STATE]"]
        lines.extend(f"{m['role']}: {m['content']}" for m in kept)
        lines.append("(delta is newer than conversation_state; prefer it when they conflict)")
        return "\n".join(lines), [str(m.get("turn_id") or "") for m in kept]

    def _aliases_from_state(self, session_id: str) -> list[str]:
        state = self.state.get(session_id)
        aliases: list[str] = []
        for ent in (state.get("entities") or {}).values():
            for a in ent.get("aliases") or []:
                aliases.append(str(a))
        return aliases

    def _demote_entities(self, session_id: str) -> set[str]:
        # demote semantic hits for entities that already have attributes (stale trap mitigation)
        state = self.state.get(session_id)
        return set((state.get("entities") or {}).keys())

    def _build_context_sync(self, *, session_id: str, query: str) -> tuple[str, MemoryContextSummary]:
        layers: list[LayerReport] = []
        blocks: list[str] = []

        state_text, entity_count = self.state.render(session_id)
        if state_text:
            blocks.append(state_text)
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="used",
                    token_chars=len(state_text),
                    item_ids=[f"entities:{entity_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="conversation_state", status="skipped"))

        delta = self._state_delta(session_id)
        if delta is not None:
            delta_text, delta_ids = delta
            blocks.append(delta_text)
            layers.append(
                LayerReport(
                    name="state_delta",
                    status="stale_delta",
                    token_chars=len(delta_text),
                    item_ids=delta_ids,
                )
            )

        curated_text, curated_count = self.curated.snapshot_text(session_id=session_id)
        if curated_text:
            blocks.append(curated_text)
            layers.append(
                LayerReport(
                    name="curated",
                    status="used",
                    token_chars=len(curated_text),
                    item_ids=[f"count:{curated_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="curated", status="skipped"))

        summary_text = self.summaries.render(session_id, limit=8)
        if summary_text:
            blocks.append(summary_text)
            layers.append(LayerReport(name="turn_summary", status="used", token_chars=len(summary_text)))
        else:
            layers.append(LayerReport(name="turn_summary", status="skipped"))

        hits = self.semantic.search(
            session_id=session_id,
            query=query,
            limit=5,
            expand_aliases=self._aliases_from_state(session_id),
            demote_entity_ids=self._demote_entities(session_id),
        )
        semantic_text = self.semantic.render(hits)
        if semantic_text:
            blocks.append(semantic_text)
            layers.append(
                LayerReport(
                    name="semantic",
                    status="used",
                    token_chars=len(semantic_text),
                    item_ids=[h["turn_id"] for h in hits],
                )
            )
        else:
            layers.append(LayerReport(name="semantic", status="skipped"))

        recent = self.transcript.recent_messages()
        if recent:
            layers.append(
                LayerReport(
                    name="recent_raw",
                    status="used",
                    token_chars=sum(len(m["content"]) for m in recent),
                    item_ids=[str(i) for i in range(len(recent))],
                )
            )
        else:
            layers.append(LayerReport(name="recent_raw", status="skipped"))

        memory_system = "\n\n".join(b for b in blocks if b)
        summary = MemoryContextSummary(
            layers=layers,
            curated_count=curated_count,
            state_entity_count=entity_count,
            recent_turn_count=len(recent) // 2,
        )
        return memory_system, summary

    async def build_context_async(self, *, session_id: str, query: str, user_id: str | None = None) -> tuple[str, MemoryContextSummary]:
        if not self.hybrid_semantic:
            return self._build_context_sync(session_id=session_id, query=query)
        layers: list[LayerReport] = []
        blocks: list[str] = []

        state_text, entity_count = self.state.render(session_id)
        if state_text:
            blocks.append(state_text)
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="used",
                    token_chars=len(state_text),
                    item_ids=[f"entities:{entity_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="conversation_state", status="skipped"))

        delta = self._state_delta(session_id)
        if delta is not None:
            delta_text, delta_ids = delta
            blocks.append(delta_text)
            layers.append(
                LayerReport(
                    name="state_delta",
                    status="stale_delta",
                    token_chars=len(delta_text),
                    item_ids=delta_ids,
                )
            )

        curated_text, curated_count = self.curated.snapshot_text(session_id=session_id)
        if curated_text:
            blocks.append(curated_text)
            layers.append(
                LayerReport(
                    name="curated",
                    status="used",
                    token_chars=len(curated_text),
                    item_ids=[f"count:{curated_count}"],
                )
            )
        else:
            layers.append(LayerReport(name="curated", status="skipped"))

        summary_text = self.summaries.render(session_id, limit=8)
        if summary_text:
            blocks.append(summary_text)
            layers.append(LayerReport(name="turn_summary", status="used", token_chars=len(summary_text)))
        else:
            layers.append(LayerReport(name="turn_summary", status="skipped"))

        hits = await self.semantic.search_hybrid(
            session_id=session_id,
            query=query,
            limit=5,
            expand_aliases=self._aliases_from_state(session_id),
            demote_entity_ids=self._demote_entities(session_id),
        )
        semantic_text = self.semantic.render(hits)
        if semantic_text:
            blocks.append(semantic_text)
            layers.append(
                LayerReport(
                    name="semantic",
                    status="used",
                    token_chars=len(semantic_text),
                    item_ids=[h["turn_id"] for h in hits],
                    notes="hybrid",
                )
            )
        else:
            layers.append(LayerReport(name="semantic", status="skipped"))

        recent = self.transcript.recent_messages()
        if recent:
            layers.append(
                LayerReport(
                    name="recent_raw",
                    status="used",
                    token_chars=sum(len(m["content"]) for m in recent),
                    item_ids=[str(i) for i in range(len(recent))],
                )
            )
        else:
            layers.append(LayerReport(name="recent_raw", status="skipped"))

        memory_system = "\n\n".join(b for b in blocks if b)
        summary = MemoryContextSummary(
            layers=layers,
            curated_count=curated_count,
            state_entity_count=entity_count,
            recent_turn_count=len(recent) // 2,
        )
        return memory_system, summary


class Memory(MemoryFacade):
    """PUBLIC_API constructors for the layered memory stack."""

    @classmethod
    def local(cls, path: str | Path = "./.ariadne/memory") -> "Memory":
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        state = ConversationStateStore(path=root / "state.json")
        return cls(
            transcript=TranscriptStore(path=root / "transcript.jsonl"),
            curated=CuratedStore(path=root / "curated.json"),
            state=state,
            summaries=TurnSummaryStore(path=root / "summaries.json"),
            semantic=SemanticIndex(path=root / "semantic.json", embedder=HashEmbeddingProvider()),
            projection=ProjectionWorker(path=root / "projection_jobs.json", state_store=state),
        )

    @classmethod
    def in_memory(cls) -> "Memory":
        """Tests only: file stores rooted in a throwaway temp dir."""
        return cls.local(path=Path(tempfile.mkdtemp(prefix="ariadne-mem-")))
