"""Deep search planners (design/memory-search.md §4.2).

Planners may only emit sub-queries, alias expansions, closed episode traversal
operations, and a reorder of **existing** candidate keys. They must never
invent turn text, facts, or identifiers.

S3 flow (two phase when using LLM):

1. ``plan`` → subqueries / alias_extra (decomp)
2. host runs subqueries and merges candidates
3. ``rerank`` → order of final candidate keys only
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .episodes import TRAVERSAL_STEPS


@dataclass
class DeepPlan:
    subqueries: list[str] = field(default_factory=list)
    alias_extra: list[str] = field(default_factory=list)
    # Closed host-executed episode traversal operations. No facts/ids in plans.
    traversal_steps: list[str] = field(default_factory=list)
    # Ordered "session_id:turn_id" keys drawn only from candidates (rerank phase)
    rerank_order: list[str] | None = None
    notes: str = ""


class DeepRerankError(Exception):
    """Raised when LLM/network/parse fails during rerank (not a no-op decision)."""

    def __init__(self, notes: str) -> None:
        super().__init__(notes)
        self.notes = notes


class DeepPlanner(Protocol):
    async def plan(
        self,
        *,
        query: str,
        aliases: list[str],
        candidates: list[dict[str, Any]],
    ) -> DeepPlan: ...

    async def rerank(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[str] | None: ...


def _exchange_text(raw: Any) -> str:
    """Extract assistant text from ModelExchange / dict / plain content."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        if "content" in raw and isinstance(raw["content"], str):
            return raw["content"]
        msg = raw.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return str(msg["content"])
        return str(raw.get("content") or "")
    # ModelExchange(message=Message(content=...))
    msg = getattr(raw, "message", None)
    if msg is not None:
        content = getattr(msg, "content", None)
        if content is not None:
            return str(content)
    content = getattr(raw, "content", None)
    if content is not None:
        return str(content)
    return str(raw)


def _parse_json_object(body: str) -> dict[str, Any] | None:
    text = (body or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


async def _model_complete(model: Any, messages: list[dict[str, Any]]) -> str:
    if hasattr(model, "complete"):
        raw = await model.complete(messages=messages, tools=None, tool_choice=None)
        return _exchange_text(raw)
    if hasattr(model, "chat"):
        raw = await model.chat(messages)
        return _exchange_text(raw)
    raise TypeError("model has no complete/chat")


class LocalSplitPlanner:
    """Deterministic local decomp: split on and/commas/和 — no LLM."""

    async def plan(
        self,
        *,
        query: str,
        aliases: list[str],
        candidates: list[dict[str, Any]],
    ) -> DeepPlan:
        _ = candidates
        parts = [
            p.strip()
            for p in re.split(r"[，,;；]|和|以及|\band\b", query or "")
            if p.strip()
        ]
        q = (query or "").casefold()
        traversal: list[str] = []
        if re.search(r"为什么|原因|why|because", q):
            traversal.extend(
                [
                    "resolve_entity",
                    "retrieve_timeline",
                    "locate_decision",
                    "expand_evidence",
                ]
            )
        if re.search(r"后来|最后|最终|是否解决|解决了吗|outcome|resolved|finally", q):
            traversal.extend(
                [
                    "resolve_entity",
                    "retrieve_timeline",
                    "locate_outcome",
                    "expand_evidence",
                ]
            )
        if re.search(r"转给|交给|负责人|assignee|assigned|related to|有关", q):
            traversal.extend(["resolve_entity", "follow_relation", "retrieve_timeline"])
        traversal = list(dict.fromkeys(traversal))
        if len(parts) <= 1:
            return DeepPlan(
                subqueries=[],
                alias_extra=list(aliases or []),
                traversal_steps=traversal,
                notes="local_traversal" if traversal else "local_noop",
            )
        return DeepPlan(
            subqueries=parts,
            alias_extra=list(aliases or []),
            traversal_steps=traversal,
            notes="local_query_split",
        )

    async def rerank(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[str] | None:
        _ = query, candidates
        return None


def make_llm_deep_planner(model: Any, *, max_candidates: int = 16) -> DeepPlanner:
    """Build a planner that asks the chat model for JSON decomp then rerank."""

    class _LlmDeepPlanner:
        async def plan(
            self,
            *,
            query: str,
            aliases: list[str],
            candidates: list[dict[str, Any]],
        ) -> DeepPlan:
            cand_lines: list[str] = []
            for c in candidates[:max_candidates]:
                key = f"{c.get('session_id')}:{c.get('turn_id')}"
                snip = str(c.get("snippet") or c.get("text") or "")[:160]
                cand_lines.append(f"- {key}: {snip}")
            system = (
                "You plan memory retrieval. Reply with JSON only: "
                '{"subqueries":["..."],"alias_extra":["..."],'
                '"traversal_steps":["resolve_entity",...]}. '
                "subqueries are short search strings to run next. "
                "traversal_steps may only use resolve_entity, follow_relation, "
                "retrieve_timeline, locate_decision, locate_outcome, expand_evidence. "
                "Do not invent dialogue or turn ids. "
                "Do not reorder hits in this step."
            )
            user = (
                f"query: {query}\n"
                f"aliases: {', '.join(aliases) if aliases else '(none)'}\n"
                f"seed candidates:\n"
                + ("\n".join(cand_lines) if cand_lines else "(none)")
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            try:
                body = await _model_complete(model, messages)
            except Exception as exc:  # noqa: BLE001
                return DeepPlan(notes=f"llm_planner_error:{type(exc).__name__}")
            obj = _parse_json_object(body)
            if obj is None:
                return DeepPlan(notes="llm_planner_parse_error")
            subs = [
                str(x).strip()
                for x in (obj.get("subqueries") or [])
                if str(x).strip()
            ]
            alias_extra = [
                str(x).strip()
                for x in (obj.get("alias_extra") or [])
                if str(x).strip()
            ]
            traversal = [
                str(x).strip()
                for x in (obj.get("traversal_steps") or [])
                if str(x).strip() in TRAVERSAL_STEPS
            ]
            # Accept optional rerank_order if the model returns it in the same
            # JSON (API surface); facade still prefers a post-merge rerank().
            allowed = {
                f"{c.get('session_id')}:{c.get('turn_id')}"
                for c in candidates
            }
            rerank: list[str] | None = None
            raw_order = obj.get("rerank_order")
            if isinstance(raw_order, list):
                filtered = [str(k) for k in raw_order if str(k) in allowed]
                rerank = filtered or None
            return DeepPlan(
                subqueries=subs[:8],
                alias_extra=alias_extra[:16],
                traversal_steps=list(dict.fromkeys(traversal))[:8],
                rerank_order=rerank,
                notes="llm_planner",
            )

        async def rerank(
            self,
            *,
            query: str,
            candidates: list[dict[str, Any]],
        ) -> list[str] | None:
            if not candidates:
                return None
            allowed_keys: list[str] = []
            cand_lines: list[str] = []
            for c in candidates[:max_candidates]:
                key = f"{c.get('session_id')}:{c.get('turn_id')}"
                allowed_keys.append(key)
                snip = str(c.get("snippet") or c.get("text") or "")[:160]
                cand_lines.append(f"- {key}: {snip}")
            system = (
                "Rerank memory search hits. Reply with JSON only: "
                '{"rerank_order":["session:turn",...]}. '
                "Use only the listed keys. Best match first. "
                'To leave order unchanged return {"rerank_order":[]}.'
            )
            user = (
                f"query: {query}\n"
                f"candidates:\n" + "\n".join(cand_lines)
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            try:
                body = await _model_complete(model, messages)
            except Exception as exc:  # noqa: BLE001
                raise DeepRerankError(
                    f"llm_rerank_error:{type(exc).__name__}"
                ) from exc
            obj = _parse_json_object(body)
            if obj is None:
                raise DeepRerankError("llm_rerank_parse_error")
            if "rerank_order" not in obj:
                raise DeepRerankError("llm_rerank_bad_shape")
            allowed = set(allowed_keys)
            rerank_raw = obj.get("rerank_order")
            if not isinstance(rerank_raw, list):
                raise DeepRerankError("llm_rerank_bad_shape")
            # Empty list = explicit legal no-op (keep score order).
            if not rerank_raw:
                return None
            order = [str(k) for k in rerank_raw if str(k) in allowed]
            return order or None

    return _LlmDeepPlanner()
