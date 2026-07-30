from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


_VAGUE_DEIXIS = re.compile(
    r"(昨天|之前|那个|上次|早先|earlier|yesterday|previous|that\s+(plan|approach|issue|bug)|the\s+one\s+we)",
    re.I,
)


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
    # Personal operator id for this facade (design/memory-scopes.md). Not multi-tenant SaaS.
    # None means explicit single-operator default "local" — never silently discard a provided id.
    user_id: str | None = "local"
    # Optional separate store for user-scope curated (e.g. ~/.ariadne/memory/curated.json)
    user_curated: CuratedStore | None = None
    # Default memory_search mode when tool omits mode
    search_mode_default: str = "auto"
    # per-layer char budgets (config, not vibes); truncation is always marked
    layer_budgets: dict[str, int] = field(
        default_factory=lambda: {
            "conversation_state": 2500,
            "curated": 1500,
            "turn_summary": 2000,
            "semantic": 1500,
        }
    )

    def resolve_user_id(self, user_id: str | None) -> str:
        """Bind request user_id; never silently drop a provided id.

        - Empty/None → facade default (usually ``local`` for single-operator CLI).
        - Facade bound to a concrete account (Web) → mismatch fastfails.
        - Facade default ``local`` → accept the request id (personal 2C; one
          operator, no multi-tenant remap).
        """
        if user_id is None or str(user_id).strip() == "":
            return self.user_id or "local"
        uid = str(user_id).strip()
        facade_uid = self.user_id or "local"
        if facade_uid == "local":
            return uid
        if uid != facade_uid:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    f"user_id {uid!r} does not match memory facade user {facade_uid!r}",
                    user_id=uid,
                    facade_user_id=facade_uid,
                )
            )
        return uid

    def _curated_for_scope(self, scope: str) -> CuratedStore:
        if scope == "user" and self.user_curated is not None:
            return self.user_curated
        return self.curated

    def _curated_snapshot(self, session_id: str) -> tuple[str, int]:
        """Merge user-scope (optional separate store) + workspace/session curated."""
        lines: list[str] = []
        count = 0
        for scope, store in (
            ("user", self._curated_for_scope("user")),
            ("workspace", self.curated),
            ("session", self.curated),
        ):
            data = store.apply(action="read", scope=scope, session_id=session_id)
            items = list(data.get("entries") or [])
            if not items:
                continue
            lines.append(f"[CURATED_DURABLE {scope}]")
            for item in items:
                eid = item.get("id", "?")
                lines.append(f"- ({eid}) {item['content']}")
                count += 1
        return "\n".join(lines), count

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
        # Resolve user_id (fastfail on mismatch — never silent ignore)
        self.resolve_user_id(user_id)
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
        self.resolve_user_id(user_id)
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
        """Convenience read for hosts: user + workspace + session curated entries."""
        uc = self._curated_for_scope("user")
        user = uc.apply(action="read", scope="user", session_id=session_id)
        workspace = self.curated.apply(
            action="read", scope="workspace", session_id=session_id
        )
        session = self.curated.apply(
            action="read", scope="session", session_id=session_id
        )
        return {
            "user": user["entries"],
            "workspace": workspace["entries"],
            "session": session["entries"],
        }

    def apply_curated(
        self,
        *,
        action: str,
        content: str = "",
        entry_ref: str = "",
        scope: str = "user",
        session_id: str,
        source_turn_id: str = "",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Route curated ops to the correct store for scope (user may be separate)."""
        self.resolve_user_id(user_id)
        scope_n = (scope or "user").strip().lower()
        store = self._curated_for_scope(scope_n)
        return store.apply(
            action=action,
            content=content,
            entry_ref=entry_ref,
            scope=scope_n if scope_n != "user" or self.user_curated is None else "user",
            session_id=session_id,
            source_turn_id=source_turn_id,
        )

    async def memory_search(
        self,
        *,
        query: str,
        session_id: str,
        scope: str = "session",
        mode: str | None = None,
        limit: int = 8,
        before_turn_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Graded episodic search (design/memory-search.md). Never invents turns."""
        self.resolve_user_id(user_id)
        q = (query or "").strip()
        if not q:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "query is required")
            )
        scope_n = (scope or "session").strip().lower()
        if scope_n not in {"session", "workspace", "user"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "scope must be session|workspace|user",
                )
            )
        mode_n = (mode or self.search_mode_default or "auto").strip().lower()
        if mode_n not in {"auto", "fast", "deep"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "mode must be auto|fast|deep",
                )
            )
        lim = max(1, min(int(limit or 8), 32))
        allowed = self._allowed_turns(session_id, before_turn_id)

        # user scope: search curated text (durable prefs), not invented history
        if scope_n == "user":
            return self._search_curated_scope(
                scope="user", query=q, session_id=session_id, limit=lim, mode_used="fast"
            )

        sid_filter: str | None = session_id if scope_n == "session" else None
        notes: list[str] = []

        async def _fast(expand: list[str] | None = None) -> list[dict[str, Any]]:
            auth = self._authoritative_fields(session_id, allowed_turn_ids=allowed)
            if self.hybrid_semantic:
                return await self.semantic.search_hybrid(
                    session_id=sid_filter,
                    query=q,
                    limit=lim,
                    expand_aliases=expand
                    or self._aliases_from_state(session_id, allowed_turn_ids=allowed),
                    demote_entity_ids=set(auth),
                    authoritative_fields=auth,
                    allowed_turn_ids=allowed if scope_n == "session" else None,
                )
            return self.semantic.search(
                session_id=sid_filter,
                query=q,
                limit=lim,
                expand_aliases=expand
                or self._aliases_from_state(session_id, allowed_turn_ids=allowed),
                demote_entity_ids=set(auth),
                authoritative_fields=auth,
                allowed_turn_ids=allowed if scope_n == "session" else None,
            )

        hits = await _fast()
        mode_used = "fast"

        def _should_upgrade(h: list[dict[str, Any]]) -> bool:
            if mode_n == "fast":
                return False
            if mode_n == "deep":
                return True
            # auto
            if not h:
                return bool(_VAGUE_DEIXIS.search(q))
            top = float(h[0].get("score") or 0)
            if top < 0.12:
                return True
            if len(h) >= 2:
                gap = top - float(h[1].get("score") or 0)
                if gap < 0.04 and top < 0.35:
                    return True
            if _VAGUE_DEIXIS.search(q) and top < 0.25:
                return True
            return False

        if mode_n in {"auto", "deep"} and _should_upgrade(hits):
            # deep without mandatory LLM: alias expand + query split + re-merge
            aliases = self._aliases_from_state(session_id, allowed_turn_ids=allowed)
            parts = [p.strip() for p in re.split(r"[，,;；]|和|以及|\band\b", q) if p.strip()]
            merged: dict[str, dict[str, Any]] = {}
            for part in parts or [q]:
                sub = await _fast(expand=aliases)
                for hit in sub:
                    key = f"{hit.get('session_id')}:{hit.get('turn_id')}"
                    prev = merged.get(key)
                    if prev is None or float(hit.get("score") or 0) > float(
                        prev.get("score") or 0
                    ):
                        merged[key] = hit
            deep_hits = sorted(
                merged.values(), key=lambda x: -float(x.get("score") or 0)
            )[:lim]
            if deep_hits:
                hits = deep_hits
                mode_used = "deep"
                notes.append("deep:alias_split_rerank")
            elif mode_n == "deep":
                notes.append("deep:no_better_hits")
            else:
                notes.append("auto:upgrade_attempted")

        # Normalize hit evidence shape
        out_hits: list[dict[str, Any]] = []
        for h in hits:
            snippet = str(h.get("snippet") or h.get("text") or "")[:400]
            out_hits.append(
                {
                    "turn_id": str(h.get("turn_id") or ""),
                    "session_id": str(h.get("session_id") or session_id),
                    "score": h.get("score"),
                    "snippet": snippet,
                    "evidence": {
                        "source": "chunk",
                        "kind": h.get("kind"),
                    },
                }
            )
        if not out_hits:
            notes.append("empty")
        return {
            "mode_used": mode_used,
            "hits": out_hits,
            "notes": "; ".join(notes) if notes else "",
            "scope": scope_n,
            "query": q,
        }

    def _search_curated_scope(
        self,
        *,
        scope: str,
        query: str,
        session_id: str,
        limit: int,
        mode_used: str,
    ) -> dict[str, Any]:
        store = self._curated_for_scope(scope)
        data = store.apply(action="read", scope=scope, session_id=session_id)
        q_tokens = set(re.findall(r"[a-z0-9_\u4e00-\u9fff]{2,}", query.lower()))
        hits: list[dict[str, Any]] = []
        for item in data.get("entries") or []:
            content = str(item.get("content") or "")
            c_low = content.lower()
            score = 0.0
            for t in q_tokens:
                if t in c_low:
                    score += 1.0
            if score <= 0 and query.lower() in c_low:
                score = 0.5
            if score > 0:
                hits.append(
                    {
                        "turn_id": str(item.get("source_turn_id") or ""),
                        "session_id": session_id if scope == "session" else "",
                        "score": round(score / max(len(q_tokens), 1), 4),
                        "snippet": content[:400],
                        "evidence": {
                            "source": "curated",
                            "entry_id": item.get("id"),
                            "scope": scope,
                        },
                    }
                )
        hits.sort(key=lambda x: -float(x.get("score") or 0))
        return {
            "mode_used": mode_used,
            "hits": hits[:limit],
            "notes": "curated_scope" if hits else "empty",
            "scope": scope,
            "query": query,
        }

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
        proj_note = "projection:disabled" if self.projection is None else ""
        notes = ", ".join(x for x in (state_note, proj_note, pit_note) if x)
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
                    status="disabled" if self.projection is None else "skipped",
                    notes=notes or pit_note,
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

        curated_text, curated_count = self._curated_snapshot(session_id)
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
        self.resolve_user_id(user_id)
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
        proj_note = "projection:disabled" if self.projection is None else ""
        notes = ", ".join(x for x in (state_note, lag_note, proj_note, pit_note) if x)
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
                    status="disabled" if self.projection is None else "skipped",
                    notes=notes,
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

        curated_text, curated_count = self._curated_snapshot(session_id)
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

        # Episodic L4 is light/budgeted in build_context; use memory_search for hard recall
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
                        notes="hybrid; field_demote; prefer memory_search for hard recall",
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
    def local(
        cls,
        path: str | Path = "./.ariadne/memory",
        *,
        enable_projection: bool = False,
        user_id: str | None = "local",
        user_curated_path: str | Path | None = None,
        search_mode_default: str = "auto",
    ) -> "Memory":
        """Local personal memory root.

        Projection is **off by default** (honest L2): without a real projector,
        jobs are not silently completed as empty ``no_change``. Pass
        ``enable_projection=True`` to create a queue for hosts that inject a
        projector (or accept tool-driven state only via ``conversation_state``).
        """
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        state = ConversationStateStore(path=root / "state.json")
        projection: ProjectionWorker | None = None
        if enable_projection:
            projection = ProjectionWorker(
                path=root / "projection_jobs.json", state_store=state
            )
        user_curated: CuratedStore | None = None
        if user_curated_path is not None:
            user_curated = CuratedStore(path=Path(user_curated_path))
        return cls(
            transcript=TranscriptStore(path=root / "transcript.jsonl"),
            curated=CuratedStore(path=root / "curated.json"),
            state=state,
            summaries=TurnSummaryStore(path=root / "summaries.json"),
            semantic=SemanticIndex(
                path=root / "semantic.json", embedder=HashEmbeddingProvider()
            ),
            projection=projection,
            user_id=user_id,
            user_curated=user_curated,
            search_mode_default=search_mode_default,
        )

    @classmethod
    def in_memory(cls, **kwargs: Any) -> "Memory":
        """Tests only: file stores rooted in a throwaway temp dir."""
        return cls.local(path=Path(tempfile.mkdtemp(prefix="ariadne-mem-")), **kwargs)
