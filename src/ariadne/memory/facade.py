from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from ..types import LayerReport, MemoryContext, MemoryContextSummary
from .curated import CuratedStore
from .capture_journal import CaptureJournalStore
from .deep_planner import DeepPlan, DeepPlanner, DeepRerankError, LocalSplitPlanner
from .embeddings import HashEmbeddingProvider
from .episodes import EpisodeStore
from .limits import DEFAULT_LAYER_BUDGETS, MemoryLimits
from .projection import ProjectionWorker
from .prospective import ProspectiveMemoryStore
from .reflection import ReflectionStore
from .semantic import SemanticIndex
from .state import ConversationStateStore
from .summary import TurnSummaryStore
from .transcript import TranscriptStore
from .user_model import UserModelStore

if False:  # pragma: no cover - typing without a runtime import cycle
    from .auto_capture import AutomaticMemoryProjector

STATE_DELTA_MAX_MESSAGES = 6
STATE_DELTA_CHAR_CAP = 2000
# design/memory-search.md: hard cap; over-cap is validation error (not silent clamp)
SEARCH_LIMIT_MAX = 32
SEARCH_LIMIT_DEFAULT = 8
SEARCH_QUERY_CHARS_MAX = 2000
SEARCH_RESPONSE_BYTES_MAX = 64_000


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
    # Compatibility overrides for direct/manual construction. Hosts should
    # configure the single ``limits`` object instead.
    recent_limit: int | None = None
    hybrid_semantic: bool = True
    require_ready: bool = False  # if True, pending projection lag fails the build
    # Personal operator id for this facade (design/memory-scopes.md). Not multi-tenant SaaS.
    # None means explicit single-operator default "local" — never silently discard a provided id.
    user_id: str | None = "local"
    # Optional separate store for user-scope curated (e.g. ~/.ariadne/memory/curated.json)
    user_curated: CuratedStore | None = None
    # Cross-workspace user episodic L4 (design S4); None → curated-only user search
    user_episodic: SemanticIndex | None = None
    # Optional deep planner (LLM or local split); None → LocalSplitPlanner only
    deep_planner: DeepPlanner | None = None
    # Default memory_search mode when tool omits mode
    search_mode_default: str = "auto"
    # Optional workspace key stamped on user-episodic chunks
    workspace_key: str = ""
    user_model: UserModelStore | None = None
    # Higher-level, evidence-bound memory intelligence. These extend rather
    # than replace the existing turn semantic index.
    episodes: EpisodeStore | None = None
    capture_journal: CaptureJournalStore | None = None
    auto_capture: Any | None = None
    reflection: ReflectionStore | None = None
    prospective: ProspectiveMemoryStore | None = None
    episode_search: bool = True
    # per-layer char budgets (config, not vibes); truncation is always marked
    layer_budgets: dict[str, int] | None = None
    # Central host configuration. Kept after the historical positional fields
    # so direct/manual construction remains source-compatible.
    limits: MemoryLimits = field(default_factory=MemoryLimits)

    def __post_init__(self) -> None:
        if self.recent_limit is None:
            self.recent_limit = self.limits.recent_limit
        else:
            self.recent_limit = int(self.recent_limit)
        if self.layer_budgets is None:
            self.layer_budgets = dict(self.limits.layer_budgets)
        else:
            # Direct callers may provide an override subset; retain the
            # defaults for every layer not explicitly overridden.
            self.layer_budgets = {
                **DEFAULT_LAYER_BUDGETS,
                **dict(self.layer_budgets),
            }

    async def capture_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        tool_calls: list[Any] | None = None,
        verified_goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.auto_capture is None:
            return {"status": "disabled"}
        return await self.auto_capture.capture_turn(
            session_id=session_id,
            turn_id=turn_id,
            workspace_key=self.workspace_key,
            user_text=user_text,
            assistant_text=assistant_text,
            tool_calls=tool_calls or [],
            verified_goal=verified_goal,
        )

    def _append_cognitive_context(
        self,
        *,
        session_id: str,
        query: str,
        blocks: list[str],
        layers: list[LayerReport],
    ) -> None:
        if self.reflection is None:
            layers.append(LayerReport(name="reflection", status="disabled"))
        else:
            try:
                text, count = self.reflection.render_pending(session_id=session_id)
                text, note = self._apply_budget("reflection", text)
                if text:
                    blocks.append(text)
                    layers.append(
                        LayerReport(
                            name="reflection",
                            status="used",
                            token_chars=len(text),
                            item_ids=[f"pending:{count}"],
                            notes=note,
                        )
                    )
                else:
                    layers.append(LayerReport(name="reflection", status="skipped"))
            except Exception as exc:  # noqa: BLE001 - optional layer, fail visible
                layers.append(
                    LayerReport(
                        name="reflection",
                        status="failed",
                        notes=f"{type(exc).__name__}: {exc}"[:200],
                    )
                )

        if self.prospective is None:
            layers.append(LayerReport(name="prospective", status="disabled"))
        else:
            try:
                query_paths = re.findall(
                    r"(?:[A-Za-z0-9_.*-]+/)+[A-Za-z0-9_.*/-]+", query or ""
                )
                self.prospective.match(
                    context={
                        "workspace": self.workspace_key,
                        "text": query,
                        "changed_paths": query_paths,
                        "tool_names": [],
                        "event_types": [],
                        "entity_ids": [],
                    }
                )
                text, count = self.prospective.render_active()
                text, note = self._apply_budget("prospective", text)
                if text:
                    blocks.append(text)
                    layers.append(
                        LayerReport(
                            name="prospective",
                            status="used",
                            token_chars=len(text),
                            item_ids=[f"triggered:{count}"],
                            notes=note,
                        )
                    )
                else:
                    layers.append(LayerReport(name="prospective", status="skipped"))
            except Exception as exc:  # noqa: BLE001 - optional layer, fail visible
                layers.append(
                    LayerReport(
                        name="prospective",
                        status="failed",
                        notes=f"{type(exc).__name__}: {exc}"[:200],
                    )
                )

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
        """Index into workspace semantic and user episodic (when configured)."""
        self.semantic.index_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
            tool_text=tool_text,
            summary_text=summary_text,
            entity_ids=entity_ids,
            workspace_key=self.workspace_key,
        )
        if self.user_episodic is not None:
            self.user_episodic.index_turn(
                session_id=session_id,
                turn_id=turn_id,
                user_text=user_text,
                assistant_text=assistant_text,
                tool_text=tool_text,
                summary_text=summary_text,
                entity_ids=entity_ids,
                workspace_key=self.workspace_key,
            )

    def resolve_user_id(self, user_id: str | None) -> str:
        """Bind request user_id; never silently drop or remap a provided id.

        - Empty/None → facade default (usually ``local`` for single-operator CLI).
        - Non-empty must match facade ``user_id`` exactly (Web account or CLI bind).
        """
        facade_uid = self.user_id or "local"
        if user_id is None or str(user_id).strip() == "":
            return facade_uid
        uid = str(user_id).strip()
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
            source_session_id=session_id,
        )

    async def memory_search(
        self,
        *,
        query: str,
        session_id: str,
        scope: str = "session",
        mode: str | None = None,
        limit: int = SEARCH_LIMIT_DEFAULT,
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
        if len(q) > SEARCH_QUERY_CHARS_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"query exceeds {SEARCH_QUERY_CHARS_MAX} characters",
                )
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
        try:
            lim_raw = int(limit if limit is not None else SEARCH_LIMIT_DEFAULT)
        except (TypeError, ValueError) as exc:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "limit must be an integer")
            ) from exc
        if lim_raw < 1:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "limit must be >= 1")
            )
        if lim_raw > SEARCH_LIMIT_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"limit {lim_raw} exceeds hard max {SEARCH_LIMIT_MAX}",
                    limit=lim_raw,
                    limit_max=SEARCH_LIMIT_MAX,
                )
            )
        lim = lim_raw
        cutoff = str(before_turn_id).strip() if before_turn_id else ""
        before_ts, allowed, asof_notes = self._resolve_as_of(
            session_id=session_id, before_turn_id=cutoff or None, scope=scope_n
        )
        notes: list[str] = list(asof_notes)
        episode_hits: list[dict[str, Any]] = []
        if self.episodes is not None and self.episode_search:
            try:
                episode_hits = self.episodes.search(
                    query=q,
                    scope=scope_n,
                    session_id=session_id,
                    workspace_key=self.workspace_key,
                    limit=lim,
                    allowed_turn_ids=allowed,
                    before_ts=before_ts,
                )
                if episode_hits:
                    notes.append("episode:search")
            except Exception as exc:  # noqa: BLE001 - search layer is optional
                notes.append(f"episode:error:{type(exc).__name__}")

        # Index selection by scope
        if scope_n == "user":
            index = self.user_episodic
            sid_filter: str | None = None
            if index is None:
                notes.append("user_episodic=unavailable")
                curated_hits = self._search_curated_hits(
                    scope="user",
                    query=q,
                    session_id=session_id,
                    limit=lim,
                    allowed_turn_ids=allowed,
                    before_ts=before_ts,
                    before_turn_id=cutoff or None,
                )
                combined = sorted(
                    curated_hits + episode_hits,
                    key=lambda row: -float(row.get("score") or 0),
                )[:lim]
                return self._pack_search_result(
                    mode_used="fast",
                    hits=combined,
                    notes=notes
                    + (
                        ["episode_curated_only", "empty"]
                        if not combined
                        else ["episode_curated_only"]
                    ),
                    scope=scope_n,
                    query=q,
                )
        else:
            index = self.semantic
            sid_filter = session_id if scope_n == "session" else None

        async def _fast(
            query_text: str,
            *,
            expand: list[str] | None = None,
            idx: SemanticIndex | None = None,
            session_filter: str | None = None,
        ) -> list[dict[str, Any]]:
            store = idx or index
            assert store is not None
            auth = self._authoritative_fields(session_id, allowed_turn_ids=allowed)
            aliases = (
                expand
                if expand is not None
                else self._aliases_from_state(session_id, allowed_turn_ids=allowed)
            )
            sf = sid_filter if session_filter is None else session_filter
            if self.hybrid_semantic:
                return await store.search_hybrid(
                    session_id=sf,
                    query=query_text,
                    limit=lim,
                    expand_aliases=aliases,
                    demote_entity_ids=set(auth),
                    authoritative_fields=auth,
                    allowed_turn_ids=allowed,
                    before_ts=before_ts,
                )
            return store.search(
                session_id=sf,
                query=query_text,
                limit=lim,
                expand_aliases=aliases,
                demote_entity_ids=set(auth),
                authoritative_fields=auth,
                allowed_turn_ids=allowed,
                before_ts=before_ts,
            )

        hits = await _fast(q)
        mode_used = "fast"

        if scope_n == "user":
            # Merge curated hits that have real turn provenance (+ as-of)
            curated_hits = self._search_curated_hits(
                scope="user",
                query=q,
                session_id=session_id,
                limit=lim,
                allowed_turn_ids=allowed,
                before_ts=before_ts,
                before_turn_id=cutoff or None,
            )
            merged: dict[str, dict[str, Any]] = {}
            for h in hits + curated_hits:
                key = f"{h.get('session_id')}:{h.get('turn_id')}"
                prev = merged.get(key)
                if prev is None or float(h.get("score") or 0) > float(
                    prev.get("score") or 0
                ):
                    merged[key] = h
            hits = sorted(
                merged.values(), key=lambda x: -float(x.get("score") or 0)
            )[:lim]

        def _merge_episode_candidates(
            current: list[dict[str, Any]], additions: list[dict[str, Any]]
        ) -> list[dict[str, Any]]:
            merged: dict[str, dict[str, Any]] = {
                f"{row.get('session_id')}:{row.get('turn_id')}": dict(row)
                for row in current
            }
            for episode_hit in additions:
                key = f"{episode_hit.get('session_id')}:{episode_hit.get('turn_id')}"
                previous = merged.get(key)
                if previous is None:
                    merged[key] = dict(episode_hit)
                    continue
                enriched = dict(previous)
                for field in (
                    "episode_id",
                    "related_turn_ids",
                    "event_ids",
                    "matched_event_ids",
                    "event_chain",
                    "citations",
                    "traversal_steps",
                    "evidence_page",
                ):
                    if episode_hit.get(field):
                        enriched[field] = episode_hit.get(field)
                enriched["kind"] = "episode"
                enriched["evidence"] = dict(episode_hit.get("evidence") or {})
                if float(episode_hit.get("score") or 0) >= float(
                    previous.get("score") or 0
                ):
                    enriched["score"] = episode_hit.get("score")
                    enriched["snippet"] = episode_hit.get("snippet")
                merged[key] = enriched
            return sorted(
                merged.values(), key=lambda row: -float(row.get("score") or 0)
            )[:lim]

        if episode_hits:
            hits = _merge_episode_candidates(hits, episode_hits)

        def _should_upgrade(h: list[dict[str, Any]]) -> bool:
            if mode_n == "fast":
                return False
            if mode_n == "deep":
                return True
            if not h:
                return bool(_VAGUE_DEIXIS.search(q)) or len(q.split()) >= 4
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
            aliases = self._aliases_from_state(session_id, allowed_turn_ids=allowed)
            planner: DeepPlanner = self.deep_planner or LocalSplitPlanner()
            seed_keys = [
                f"{h.get('session_id')}:{h.get('turn_id')}" for h in hits
            ]
            seed_order = list(seed_keys)
            try:
                plan: DeepPlan = await planner.plan(
                    query=q, aliases=aliases, candidates=hits
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"deep:planner_error:{type(exc).__name__}")
                notes.append("deep:fallback_fast")
                plan = DeepPlan(notes="planner_raised")
            expand = list(aliases or []) + list(plan.alias_extra or [])
            subqs = [s for s in (plan.subqueries or []) if s.strip()]
            traversal_steps = [
                step for step in (plan.traversal_steps or []) if str(step).strip()
            ]
            planner_failed = (
                "error" in (plan.notes or "")
                or "parse" in (plan.notes or "")
                or plan.notes == "planner_raised"
            )
            if plan.notes == "local_noop" and not subqs and not traversal_steps:
                notes.append("deep:unavailable_local_noop")
                if self.deep_planner is None:
                    notes.append("deep:no_llm_planner")
            elif planner_failed and not subqs:
                notes.append(plan.notes or "deep:planner_empty")
                notes.append("deep:fallback_fast")
            else:
                if plan.notes:
                    notes.append(f"deep:{plan.notes}")
                # Phase 1: optional subqueries (may be empty → rerank-only path)
                merged_h: dict[str, dict[str, Any]] = {
                    f"{h.get('session_id')}:{h.get('turn_id')}": h for h in hits
                }
                ran_subqueries = bool(subqs)
                for part in subqs:
                    sub = await _fast(part, expand=expand or None)
                    for hit in sub:
                        key = f"{hit.get('session_id')}:{hit.get('turn_id')}"
                        prev = merged_h.get(key)
                        if prev is None or float(hit.get("score") or 0) > float(
                            prev.get("score") or 0
                        ):
                            merged_h[key] = hit
                ran_traversal = False
                if traversal_steps and self.episodes is not None and self.episode_search:
                    traversal_hits = self.episodes.traverse(
                        query=q,
                        steps=traversal_steps,
                        scope=scope_n,
                        session_id=session_id,
                        workspace_key=self.workspace_key,
                        limit=lim,
                        allowed_turn_ids=allowed,
                        before_ts=before_ts,
                    )
                    if traversal_hits:
                        ran_traversal = True
                        notes.append(
                            "deep:traversal=" + ",".join(traversal_steps)
                        )
                        for hit in _merge_episode_candidates(
                            list(merged_h.values()), traversal_hits
                        ):
                            key = f"{hit.get('session_id')}:{hit.get('turn_id')}"
                            merged_h[key] = hit
                deep_hits = list(merged_h.values())
                deep_hits.sort(key=lambda x: -float(x.get("score") or 0))
                order_after_sub = [
                    f"{h.get('session_id')}:{h.get('turn_id')}" for h in deep_hits
                ]
                set_changed = set(order_after_sub) != set(seed_keys)
                order_changed_sub = order_after_sub != seed_order
                # Phase 2: require DeepPlanner.rerank (no plan-only compatibility)
                rerank_order: list[str] | None = None
                did_rerank = False
                rerank_failed = False
                try:
                    rerank_order = await planner.rerank(
                        query=q, candidates=deep_hits
                    )
                except DeepRerankError as exc:
                    notes.append(f"deep:{exc.notes}")
                    notes.append("deep:rerank_failed")
                    notes.append("deep:rerank_fallback_score_order")
                    rerank_failed = True
                    rerank_order = None
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"deep:rerank_error:{type(exc).__name__}")
                    notes.append("deep:rerank_failed")
                    notes.append("deep:rerank_fallback_score_order")
                    rerank_failed = True
                    rerank_order = None
                if rerank_order:
                    order = {k: i for i, k in enumerate(rerank_order)}
                    deep_hits.sort(
                        key=lambda h: (
                            order.get(
                                f"{h.get('session_id')}:{h.get('turn_id')}",
                                10_000,
                            ),
                            -float(h.get("score") or 0),
                        )
                    )
                    final_order = [
                        f"{h.get('session_id')}:{h.get('turn_id')}"
                        for h in deep_hits
                    ]
                    did_rerank = final_order != order_after_sub
                    if did_rerank:
                        notes.append("deep:rerank")
                hits = deep_hits[:lim]
                final_keys = [
                    f"{h.get('session_id')}:{h.get('turn_id')}" for h in hits
                ]
                set_changed = set(final_keys) != set(seed_keys)
                order_changed = final_keys != seed_order[: len(final_keys)]
                # Honest mode_used: deep when decomp/rerank changed results.
                # Rerank failure does not demote deep if subqueries already changed.
                if set_changed or order_changed or did_rerank or ran_traversal:
                    mode_used = "deep"
                    if ran_subqueries and self.deep_planner is None:
                        notes.append("deep:local_query_split")
                        notes.append("deep:no_llm_planner")
                    if did_rerank and not ran_subqueries:
                        notes.append("deep:rerank_only")
                else:
                    mode_used = "fast"
                    if ran_subqueries:
                        notes.append("deep:noop_unchanged")
                    elif rerank_failed:
                        notes.append("deep:fallback_fast")
                    elif plan.notes and "local_noop" not in (plan.notes or ""):
                        notes.append("deep:no_work")

        # Normalize hit evidence — require real turn_id; no curated: synthetic ids
        out_hits: list[dict[str, Any]] = []
        for h in hits:
            tid = str(h.get("turn_id") or "").strip()
            if not tid or tid.startswith("curated:"):
                notes.append("dropped_hit_missing_turn_id")
                continue
            sid = str(h.get("session_id") or "").strip()
            if not sid and scope_n != "user":
                sid = session_id
            if not sid:
                notes.append("dropped_hit_missing_session_id")
                continue
            snippet = str(h.get("snippet") or h.get("text") or "")[:400]
            src = "chunk"
            ev = h.get("evidence") if isinstance(h.get("evidence"), dict) else {}
            if ev.get("source") in {"raw", "summary", "chunk", "curated", "episode"}:
                src = str(ev.get("source"))
            elif h.get("kind") in {"user", "assistant", "tool"}:
                src = "raw"
            elif h.get("kind") == "summary":
                src = "summary"
            elif h.get("kind") == "curated":
                src = "curated"
            evidence: dict[str, Any] = {
                "source": src,
                "kind": h.get("kind"),
            }
            if ev.get("entry_id"):
                evidence["entry_id"] = ev.get("entry_id")
            if ev.get("scope"):
                evidence["scope"] = ev.get("scope")
            if ev.get("chunk_id"):
                evidence["chunk_id"] = ev.get("chunk_id")
            normalized_hit = {
                    "turn_id": tid,
                    "session_id": sid,
                    "score": h.get("score"),
                    "snippet": snippet,
                    "evidence": evidence,
                }
            for field in (
                "episode_id",
                "related_turn_ids",
                "event_ids",
                "matched_event_ids",
                "event_chain",
                "citations",
                "traversal_steps",
                "evidence_page",
            ):
                if h.get(field) is not None:
                    normalized_hit[field] = h.get(field)
            out_hits.append(normalized_hit)
        if not out_hits:
            notes.append("empty")
        return self._pack_search_result(
            mode_used=mode_used, hits=out_hits, notes=notes, scope=scope_n, query=q
        )

    def expand_episode_evidence(
        self,
        *,
        episode_id: str,
        session_id: str,
        scope: str = "session",
        after_event_id: str = "",
        limit: int = 8,
        before_turn_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.resolve_user_id(user_id)
        if self.episodes is None or not self.episode_search:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "episode memory is not configured")
            )
        episode = (episode_id or "").strip()
        if not episode:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "episode_id is required")
            )
        scope_n = (scope or "session").strip().lower()
        if scope_n not in {"session", "workspace", "user"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "scope must be session|workspace|user",
                )
            )
        try:
            page_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "limit must be an integer")
            ) from exc
        cutoff = str(before_turn_id).strip() if before_turn_id else ""
        before_ts, allowed, _notes = self._resolve_as_of(
            session_id=session_id,
            before_turn_id=cutoff or None,
            scope=scope_n,
        )
        return self.episodes.expand_evidence(
            episode_id=episode,
            scope=scope_n,
            session_id=session_id,
            workspace_key=self.workspace_key,
            after_event_id=after_event_id,
            limit=page_limit,
            allowed_turn_ids=allowed,
            before_ts=before_ts,
        )

    def _pack_search_result(
        self,
        *,
        mode_used: str,
        hits: list[dict[str, Any]],
        notes: list[str],
        scope: str,
        query: str,
    ) -> dict[str, Any]:
        seen_n: set[str] = set()
        notes_u: list[str] = []
        for n in notes:
            if n and n not in seen_n:
                seen_n.add(n)
                notes_u.append(n)
        packed_hits: list[dict[str, Any]] = []
        omitted_hits = 0
        base = {
            "mode_used": mode_used,
            "hits": packed_hits,
            "notes": "; ".join(notes_u) if notes_u else "",
            "scope": scope,
            "query": query,
            "budget": {
                "max_bytes": SEARCH_RESPONSE_BYTES_MAX,
                "returned_bytes": 0,
                "truncated": False,
                "omitted_hits": 0,
            },
        }
        for index, hit in enumerate(hits):
            probe = {**base, "hits": [*packed_hits, hit]}
            size = len(
                json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if size > SEARCH_RESPONSE_BYTES_MAX:
                omitted_hits = len(hits) - index
                break
            packed_hits.append(hit)
        if omitted_hits:
            base["notes"] = "; ".join(
                [note for note in (base["notes"], "response_budget_truncated") if note]
            )
            base["budget"]["truncated"] = True
            base["budget"]["omitted_hits"] = omitted_hits
        while True:
            for _ in range(2):
                base["budget"]["returned_bytes"] = len(
                    json.dumps(base, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
            if int(base["budget"]["returned_bytes"]) <= SEARCH_RESPONSE_BYTES_MAX:
                break
            if not packed_hits:
                break
            packed_hits.pop()
            omitted_hits += 1
            base["budget"]["truncated"] = True
            base["budget"]["omitted_hits"] = omitted_hits
            if "response_budget_truncated" not in base["notes"]:
                base["notes"] = "; ".join(
                    [
                        note
                        for note in (base["notes"], "response_budget_truncated")
                        if note
                    ]
                )
        if int(base["budget"]["returned_bytes"]) > SEARCH_RESPONSE_BYTES_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_EVIDENCE_BUDGET",
                    "memory search metadata exceeds the response byte budget",
                    max_bytes=SEARCH_RESPONSE_BYTES_MAX,
                )
            )
        return base

    def _resolve_as_of(
        self,
        *,
        session_id: str,
        before_turn_id: str | None,
        scope: str,
    ) -> tuple[float | None, set[str] | None, list[str]]:
        """Resolve before_turn_id to before_ts + optional allowed turn ids."""
        notes: list[str] = []
        if not before_turn_id:
            return None, None, notes
        tid = before_turn_id.strip()
        # Prefer clock from semantic / user_episodic indexes (cross-session).
        clock = self.semantic.lookup_turn_clock(turn_id=tid, session_id=session_id)
        if clock is None and scope != "session":
            clock = self.semantic.lookup_turn_clock(turn_id=tid, session_id=None)
        if clock is None and self.user_episodic is not None:
            clock = self.user_episodic.lookup_turn_clock(
                turn_id=tid, session_id=session_id
            ) or self.user_episodic.lookup_turn_clock(turn_id=tid, session_id=None)
        before_ts = clock[0] if clock else None
        if before_ts is not None:
            notes.append(f"before_ts:{before_ts}")
            # When we have a global clock, do not also filter by turn-id set
            # (other sessions' turn_ids are not in the active transcript order).
            return before_ts, None, notes
        # Fallback: transcript order (session / global file)
        if scope == "session":
            allowed = self._allowed_turns(session_id, tid)
        else:
            allowed = self.transcript.turn_ids_before(tid, session_id=None)
            sess_allowed = self._allowed_turns(session_id, tid)
            if allowed is not None and sess_allowed is not None:
                allowed = set(allowed) | set(sess_allowed)
            elif sess_allowed is not None:
                allowed = sess_allowed
        notes.append("before_turn_id:transcript_order")
        if allowed is None:
            notes.append("clock:missing")
        return None, allowed, notes

    def _search_curated_hits(
        self,
        *,
        scope: str,
        query: str,
        session_id: str,
        limit: int,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
        before_turn_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Curated hits with real turn provenance; honor as-of clocks strictly."""
        store = self._curated_for_scope(scope)
        data = store.apply(action="read", scope=scope, session_id=session_id)
        q_tokens = set(re.findall(r"[a-z0-9_\u4e00-\u9fff]{1,}", query.lower()))
        cutoff_tid = (before_turn_id or "").strip()
        hits: list[dict[str, Any]] = []
        for item in data.get("entries") or []:
            eid = str(item.get("id") or "").strip()
            src_turn = str(item.get("source_turn_id") or "").strip()
            src_session = str(item.get("source_session_id") or "").strip()
            # Provenance required: real source turn + known session (no query-session fallback)
            if not src_turn or not src_session:
                continue
            if cutoff_tid and src_turn == cutoff_tid:
                continue
            if allowed_turn_ids is not None and src_turn not in allowed_turn_ids:
                continue
            # Strict as-of for curated:
            # - With before_ts: require updated_at < before_ts AND source-turn clock
            # - Without before_ts (legacy transcript-order only): cannot prove
            #   write time vs cutoff → exclude all curated for this search.
            if before_turn_id and before_ts is None:
                continue
            if before_ts is not None:
                updated_at = item.get("updated_at")
                if updated_at is None or float(updated_at) >= float(before_ts):
                    continue
                clock = self._lookup_turn_clock(src_turn, session_id=src_session)
                if clock is None:
                    continue
                if float(clock[0]) >= float(before_ts):
                    continue
            content = str(item.get("content") or "")
            c_low = content.lower()
            score = 0.0
            for t in q_tokens:
                if len(t) >= 1 and t in c_low:
                    score += 1.0
            if score <= 0 and query.lower() in c_low:
                score = 0.5
            if score <= 0:
                continue
            hits.append(
                {
                    "turn_id": src_turn,
                    "session_id": src_session,
                    "score": round(score / max(len(q_tokens), 1), 4),
                    "snippet": content[:400],
                    "kind": "curated",
                    "evidence": {
                        "source": "curated",
                        "entry_id": eid,
                        "scope": scope,
                    },
                }
            )
        hits.sort(key=lambda x: -float(x.get("score") or 0))
        return hits[:limit]

    def _lookup_turn_clock(
        self, turn_id: str, *, session_id: str | None = None
    ) -> tuple[float, int] | None:
        clock = self.semantic.lookup_turn_clock(
            turn_id=turn_id, session_id=session_id
        )
        if clock is None and session_id is not None:
            clock = self.semantic.lookup_turn_clock(turn_id=turn_id, session_id=None)
        if clock is None and self.user_episodic is not None:
            clock = self.user_episodic.lookup_turn_clock(
                turn_id=turn_id, session_id=session_id
            ) or self.user_episodic.lookup_turn_clock(
                turn_id=turn_id, session_id=None
            )
        return clock

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

        user_model_text, user_model_count = (
            self.user_model.render(
                workspace_key=self.workspace_key,
                session_id=session_id,
            )
            if self.user_model is not None
            else ("", 0)
        )
        user_model_text, user_model_note = self._apply_budget(
            "user_model", user_model_text
        )
        if user_model_text:
            blocks.append(user_model_text)
            layers.append(
                LayerReport(
                    name="user_model",
                    status="used",
                    token_chars=len(user_model_text),
                    item_ids=[f"count:{user_model_count}"],
                    notes=user_model_note,
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="user_model",
                    status="disabled" if self.user_model is None else "skipped",
                )
            )

        self._append_cognitive_context(
            session_id=session_id,
            query=query,
            blocks=blocks,
            layers=layers,
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

        user_model_text, user_model_count = (
            self.user_model.render(
                workspace_key=self.workspace_key,
                session_id=session_id,
            )
            if self.user_model is not None
            else ("", 0)
        )
        user_model_text, user_model_note = self._apply_budget(
            "user_model", user_model_text
        )
        if user_model_text:
            blocks.append(user_model_text)
            layers.append(
                LayerReport(
                    name="user_model",
                    status="used",
                    token_chars=len(user_model_text),
                    item_ids=[f"count:{user_model_count}"],
                    notes=user_model_note,
                )
            )
        else:
            layers.append(
                LayerReport(
                    name="user_model",
                    status="disabled" if self.user_model is None else "skipped",
                )
            )

        self._append_cognitive_context(
            session_id=session_id,
            query=query,
            blocks=blocks,
            layers=layers,
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
        user_episodic_path: str | Path | None = None,
        search_mode_default: str = "auto",
        deep_planner: DeepPlanner | None = None,
        limits: MemoryLimits | None = None,
    ) -> "Memory":
        """Local personal memory root.

        Projection is **off by default** (honest L2). User episodic index is
        enabled when ``user_episodic_path`` is set, or defaults under
        ``user_curated_path`` parent / ``episodic/semantic.json``, or
        ``{path}/episodic/semantic.json`` for single-root tests.
        """
        root = Path(path)
        root.mkdir(parents=True, exist_ok=True)
        memory_limits = limits or MemoryLimits()
        state = ConversationStateStore(path=root / "state.json")
        projection: ProjectionWorker | None = None
        if enable_projection:
            projection = ProjectionWorker(
                path=root / "projection_jobs.json", state_store=state
            )
        user_curated: CuratedStore | None = None
        if user_curated_path is not None:
            user_curated = CuratedStore(path=Path(user_curated_path))
        embedder = HashEmbeddingProvider()
        # Default user episodic for personal use: under path so scope=user works
        # in tests without extra wiring.
        if user_episodic_path is not None:
            ep_path = Path(user_episodic_path)
        elif user_curated_path is not None:
            ep_path = Path(user_curated_path).parent / "episodic" / "semantic.json"
        else:
            ep_path = root / "episodic" / "semantic.json"
        user_episodic = SemanticIndex(path=ep_path, embedder=embedder)
        from .auto_capture import AutomaticMemoryProjector

        episodes = EpisodeStore(
            path=root / "episodes.json",
            max_episodes=memory_limits.episode_max_episodes,
            max_events_per_episode=memory_limits.episode_max_events_per_episode,
        )
        capture_journal = CaptureJournalStore(
            path=root / "capture_journal.json",
            max_records=memory_limits.capture_max_records,
        )
        reflection = ReflectionStore(path=root / "reflection.json")
        prospective = ProspectiveMemoryStore(path=root / "prospective.json")
        user_model = UserModelStore(path=root / "user_model.json")
        auto_capture = AutomaticMemoryProjector(
            episodes=episodes,
            user_model=user_model,
            journal=capture_journal,
            state=state,
            reflection=reflection,
            prospective=prospective,
            resume_batch_size=memory_limits.capture_resume_batch_size,
        )
        return cls(
            transcript=TranscriptStore(path=root / "transcript.jsonl"),
            curated=CuratedStore(path=root / "curated.json"),
            state=state,
            summaries=TurnSummaryStore(path=root / "summaries.json"),
            semantic=SemanticIndex(path=root / "semantic.json", embedder=embedder),
            projection=projection,
            user_id=user_id,
            user_curated=user_curated,
            user_episodic=user_episodic,
            user_model=user_model,
            episodes=episodes,
            capture_journal=capture_journal,
            auto_capture=auto_capture,
            reflection=reflection,
            prospective=prospective,
            deep_planner=deep_planner,
            search_mode_default=search_mode_default,
            limits=memory_limits,
        )

    @classmethod
    def in_memory(cls, **kwargs: Any) -> "Memory":
        """Tests only: file stores rooted in a throwaway temp dir."""
        return cls.local(path=Path(tempfile.mkdtemp(prefix="ariadne-mem-")), **kwargs)
