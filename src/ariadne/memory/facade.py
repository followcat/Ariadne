from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AriadneError, app_error
from ..types import LayerReport, MemoryContext, MemoryContextSummary
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
    require_ready: bool = False  # if True, pending projection lag fails the build
    # per-layer char budgets (config, not vibes); truncation is always marked
    layer_budgets: dict[str, int] = field(
        default_factory=lambda: {
            "conversation_state": 2500,
            "curated": 1500,
            "turn_summary": 2000,
            "semantic": 1500,
        }
    )

    def _apply_budget(self, name: str, text: str) -> tuple[str, str]:
        """Clamp a layer block to its configured budget with an explicit marker."""
        budget = self.layer_budgets.get(name)
        if not text or budget is None or len(text) <= budget:
            return text, ""
        return (
            text[:budget] + f"\n[ariadne: layer {name} truncated to budget {budget} chars]",
            f"budget:{budget}",
        )

    def build_context(
        self,
        *,
        session_id: str,
        query: str,
        user_id: str | None = None,
        before_turn_id: str | None = None,
        require_ready: bool | None = None,
    ) -> tuple[str, MemoryContextSummary]:
        # sync wrapper for simple callers
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.build_context_async(
                    session_id=session_id,
                    user_id=user_id,
                    query=query,
                    before_turn_id=before_turn_id,
                    require_ready=require_ready,
                )
            )
        # if already in loop, fall back to lexical path without await hybrid
        return self._build_context_sync(
            session_id=session_id,
            query=query,
            before_turn_id=before_turn_id,
            require_ready=require_ready,
        )

    async def build_memory_context(
        self,
        *,
        session_id: str,
        query: str,
        user_id: str | None = None,
        before_turn_id: str | None = None,
        require_ready: bool | None = None,
    ) -> MemoryContext:
        """Normative Read API: structured MemoryContext (MEMORY §4)."""
        text, summary = await self.build_context_async(
            session_id=session_id,
            query=query,
            user_id=user_id,
            before_turn_id=before_turn_id,
            require_ready=require_ready,
        )
        return MemoryContext(
            system_text=text,
            summary=summary,
            before_turn_id=before_turn_id,
            require_ready=bool(
                self.require_ready if require_ready is None else require_ready
            ),
        )

    def recent_messages(
        self,
        *,
        session_id: str | None = None,
        before_turn_id: str | None = None,
    ) -> list[dict[str, str]]:
        """L0 recent raw window honoring the facade's configured limit."""
        if before_turn_id is None:
            return self.transcript.recent_messages(
                limit=self.recent_limit, session_id=session_id
            )
        return self.transcript.recent_messages_before(
            before_turn_id, limit=self.recent_limit, session_id=session_id
        )

    def get_curated(self, *, session_id: str) -> dict[str, object]:
        """Convenience read for hosts: user-scope + session-scope curated entries."""
        user = self.curated.apply(action="read", scope="user", session_id=session_id)
        session = self.curated.apply(action="read", scope="session", session_id=session_id)
        return {"user": user["entries"], "session": session["entries"]}

    def _allowed_turns(
        self, session_id: str, before_turn_id: str | None
    ) -> set[str] | None:
        return self.transcript.turn_ids_before(before_turn_id, session_id=session_id)

    def _state_delta(
        self, session_id: str, *, before_turn_id: str | None = None
    ) -> tuple[str, list[str]] | None:
        """last_good_plus_delta read mode: raw turns newer than the state watermark.

        Rendered state is always the last succeeded projection (last-good). Raw
        turns after the watermark are appended as a bounded delta block that
        takes precedence on conflict, so projection lag never blocks or lies.
        When ``before_turn_id`` is set, delta turns must also be before the cutoff.
        """
        if before_turn_id is not None:
            # Point-in-time reads freeze state; skip live delta past the cutoff.
            return None
        watermark = self.state.watermark(session_id)
        if watermark is None:
            return None
        records = self.transcript.records_after(watermark, session_id=session_id)
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

    def _aliases_from_state(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> list[str]:
        state = self.state.get_as_of(session_id, allowed_turn_ids=allowed_turn_ids)
        aliases: list[str] = []
        for ent in (state.get("entities") or {}).values():
            for a in ent.get("aliases") or []:
                aliases.append(str(a))
        return aliases

    def _authoritative_fields(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> dict[str, dict[str, object]]:
        """entity_id → attributes dict for field-level demotion."""
        state = self.state.get_as_of(session_id, allowed_turn_ids=allowed_turn_ids)
        out: dict[str, dict[str, object]] = {}
        for eid, ent in (state.get("entities") or {}).items():
            attrs = ent.get("attributes") or {}
            if attrs:
                out[str(eid)] = dict(attrs)
        return out

    def _demote_entities(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> set[str]:
        """Entities that have attributes (legacy entity-level demote set)."""
        return set(self._authoritative_fields(session_id, allowed_turn_ids=allowed_turn_ids))

    def entities_mentioned_in_text(self, session_id: str, text: str) -> list[str]:
        """Entity ids referenced in text via id, alias, or string attribute values.

        Used for semantic index tagging so demotion/hits stay turn-grounded
        (not the entire state entity set).
        """
        hay = (text or "").lower()
        if not hay:
            return []
        state = self.state.get(session_id)
        found: list[str] = []
        for eid, ent in (state.get("entities") or {}).items():
            surface: list[str] = [str(eid)] + [str(a) for a in (ent.get("aliases") or [])]
            for attr in (ent.get("attributes") or {}).values():
                if isinstance(attr, dict):
                    val = attr.get("value")
                else:
                    val = attr
                if isinstance(val, (str, int, float)):
                    surface.append(str(val))
            for name in surface:
                n = name.strip()
                if len(n) < 2:
                    continue
                if n.lower() in hay:
                    found.append(str(eid))
                    break
        return found

    def _check_require_ready(self, session_id: str, *, require_ready: bool | None) -> None:
        flag = self.require_ready if require_ready is None else require_ready
        if not flag or self.projection is None:
            return
        lag = self.projection.pending_lag(session_id)
        if lag > 0:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_NOT_READY",
                    f"conversation_state projection lag: {lag} pending job(s)",
                    session_id=session_id,
                    pending_jobs=lag,
                )
            )

    def _build_context_sync(
        self,
        *,
        session_id: str,
        query: str,
        before_turn_id: str | None = None,
        require_ready: bool | None = None,
    ) -> tuple[str, MemoryContextSummary]:
        # Sync path: lexical only (no await). Note hybrid_skipped for hosts.
        self._check_require_ready(session_id, require_ready=require_ready)
        allowed = self._allowed_turns(session_id, before_turn_id)
        layers: list[LayerReport] = []
        blocks: list[str] = []
        pit_note = f"before_turn_id:{before_turn_id}" if before_turn_id else ""

        state_text, entity_count = self.state.render(
            session_id, allowed_turn_ids=allowed
        )
        state_text, state_note = self._apply_budget("conversation_state", state_text)
        notes = ", ".join(x for x in (state_note, pit_note) if x)
        if state_text:
            blocks.append(state_text)
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="used",
                    token_chars=len(state_text),
                    item_ids=[f"entities:{entity_count}"],
                    notes=notes,
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="skipped",
                    notes=pit_note,
                )
            )

        delta = self._state_delta(session_id, before_turn_id=before_turn_id)
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
        curated_text, _ = self._apply_budget("curated", curated_text)
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

        summary_text = self.summaries.render(
            session_id, limit=8, allowed_turn_ids=allowed
        )
        summary_text, _ = self._apply_budget("turn_summary", summary_text)
        if summary_text:
            blocks.append(summary_text)
            layers.append(LayerReport(name="turn_summary", status="used", token_chars=len(summary_text)))
        else:
            layers.append(LayerReport(name="turn_summary", status="skipped"))

        auth_fields = self._authoritative_fields(session_id, allowed_turn_ids=allowed)
        hits = self.semantic.search(
            session_id=session_id,
            query=query,
            limit=5,
            expand_aliases=self._aliases_from_state(
                session_id, allowed_turn_ids=allowed
            ),
            demote_entity_ids=set(auth_fields),
            authoritative_fields=auth_fields,
            allowed_turn_ids=allowed,
        )
        semantic_text = self.semantic.render(hits)
        semantic_text, _ = self._apply_budget("semantic", semantic_text)
        if semantic_text:
            blocks.append(semantic_text)
            layers.append(
                LayerReport(
                    name="semantic",
                    status="used",
                    token_chars=len(semantic_text),
                    item_ids=[h["turn_id"] for h in hits],
                    notes="field_demote; hybrid_skipped: sync_path",
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="semantic",
                    status="skipped",
                    notes="hybrid_skipped: sync_path",
                )
            )

        recent = self.recent_messages(
            session_id=session_id, before_turn_id=before_turn_id
        )
        if recent:
            layers.append(
                LayerReport(
                    name="recent_raw",
                    status="used",
                    token_chars=sum(len(m["content"]) for m in recent),
                    item_ids=[str(i) for i in range(len(recent))],
                    notes="hybrid_skipped: sync_path",
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="recent_raw", status="skipped", notes="hybrid_skipped: sync_path"
                )
            )

        memory_system = "\n\n".join(b for b in blocks if b)
        summary = MemoryContextSummary(
            layers=layers,
            curated_count=curated_count,
            state_entity_count=entity_count,
            recent_turn_count=len(recent) // 2,
        )
        return memory_system, summary

    async def build_context_async(
        self,
        *,
        session_id: str,
        query: str,
        user_id: str | None = None,
        before_turn_id: str | None = None,
        require_ready: bool | None = None,
    ) -> tuple[str, MemoryContextSummary]:
        _ = user_id
        if not self.hybrid_semantic:
            return self._build_context_sync(
                session_id=session_id,
                query=query,
                before_turn_id=before_turn_id,
                require_ready=require_ready,
            )
        self._check_require_ready(session_id, require_ready=require_ready)
        allowed = self._allowed_turns(session_id, before_turn_id)
        layers: list[LayerReport] = []
        blocks: list[str] = []
        pit_note = f"before_turn_id:{before_turn_id}" if before_turn_id else ""

        state_text, entity_count = self.state.render(
            session_id, allowed_turn_ids=allowed
        )
        state_text, state_note = self._apply_budget("conversation_state", state_text)
        lag = self.projection.pending_lag(session_id) if self.projection else 0
        lag_note = f"projection_lag:{lag}" if lag else ""
        notes = ", ".join(x for x in (state_note, lag_note, pit_note) if x)
        if state_text:
            blocks.append(state_text)
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="used",
                    token_chars=len(state_text),
                    item_ids=[f"entities:{entity_count}"],
                    notes=notes,
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="conversation_state",
                    status="skipped",
                    notes=", ".join(x for x in (lag_note, pit_note) if x),
                )
            )

        delta = self._state_delta(session_id, before_turn_id=before_turn_id)
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
        curated_text, _ = self._apply_budget("curated", curated_text)
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

        summary_text = self.summaries.render(
            session_id, limit=8, allowed_turn_ids=allowed
        )
        summary_text, _ = self._apply_budget("turn_summary", summary_text)
        if summary_text:
            blocks.append(summary_text)
            layers.append(LayerReport(name="turn_summary", status="used", token_chars=len(summary_text)))
        else:
            layers.append(LayerReport(name="turn_summary", status="skipped"))

        auth_fields = self._authoritative_fields(session_id, allowed_turn_ids=allowed)
        try:
            hits = await self.semantic.search_hybrid(
                session_id=session_id,
                query=query,
                limit=5,
                expand_aliases=self._aliases_from_state(
                    session_id, allowed_turn_ids=allowed
                ),
                demote_entity_ids=set(auth_fields),
                authoritative_fields=auth_fields,
                allowed_turn_ids=allowed,
            )
            semantic_text = self.semantic.render(hits)
            semantic_text, _ = self._apply_budget("semantic", semantic_text)
            if semantic_text:
                blocks.append(semantic_text)
                layers.append(
                    LayerReport(
                        name="semantic",
                        status="used",
                        token_chars=len(semantic_text),
                        item_ids=[h["turn_id"] for h in hits],
                        notes="hybrid; field_demote",
                    )
                )
            else:
                layers.append(LayerReport(name="semantic", status="skipped"))
        except Exception as exc:  # noqa: BLE001 — layer-local fail (MEMORY fail-visible)
            layers.append(
                LayerReport(
                    name="semantic",
                    status="failed",
                    notes=f"{type(exc).__name__}: {exc}"[:200],
                )
            )

        recent = self.recent_messages(
            session_id=session_id, before_turn_id=before_turn_id
        )
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
