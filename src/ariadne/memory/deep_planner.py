"""Deep search planners (design/memory-search.md §4.2).

Planners may only emit sub-queries, alias expansions, and a reorder of
**existing** candidate keys. They must never invent turn text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class DeepPlan:
    subqueries: list[str] = field(default_factory=list)
    alias_extra: list[str] = field(default_factory=list)
    # Ordered "session_id:turn_id" keys drawn only from candidates
    rerank_order: list[str] | None = None
    notes: str = ""


class DeepPlanner(Protocol):
    async def plan(
        self,
        *,
        query: str,
        aliases: list[str],
        candidates: list[dict[str, Any]],
    ) -> DeepPlan: ...


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
        if len(parts) <= 1:
            return DeepPlan(subqueries=[], alias_extra=list(aliases or []), notes="local_noop")
        return DeepPlan(
            subqueries=parts,
            alias_extra=list(aliases or []),
            notes="local_query_split",
        )


def make_llm_deep_planner(model: Any, *, max_candidates: int = 16) -> DeepPlanner:
    """Build a planner that asks the chat model for JSON decomp/rerank only."""

    class _LlmDeepPlanner:
        async def plan(
            self,
            *,
            query: str,
            aliases: list[str],
            candidates: list[dict[str, Any]],
        ) -> DeepPlan:
            allowed_keys: list[str] = []
            cand_lines: list[str] = []
            for c in candidates[:max_candidates]:
                key = f"{c.get('session_id')}:{c.get('turn_id')}"
                allowed_keys.append(key)
                snip = str(c.get("snippet") or c.get("text") or "")[:160]
                cand_lines.append(f"- {key}: {snip}")
            system = (
                "You plan memory retrieval. Reply with JSON only: "
                '{"subqueries":["..."],"alias_extra":["..."],"rerank_order":["session:turn",...]} '
                "subqueries are short search strings. "
                "rerank_order may only use candidate keys listed. "
                "Do not invent dialogue or turn ids not listed."
            )
            user = (
                f"query: {query}\n"
                f"aliases: {', '.join(aliases) if aliases else '(none)'}\n"
                f"candidates:\n" + ("\n".join(cand_lines) if cand_lines else "(none)")
            )
            # ModelPort.chat(messages) style used elsewhere
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            try:
                if hasattr(model, "complete"):
                    raw = await model.complete(messages=messages)
                    text = (
                        raw.get("content")
                        if isinstance(raw, dict)
                        else getattr(raw, "content", None) or str(raw)
                    )
                elif hasattr(model, "chat"):
                    raw = await model.chat(messages)
                    text = (
                        raw.get("content")
                        if isinstance(raw, dict)
                        else getattr(raw, "content", None) or str(raw)
                    )
                else:
                    raise TypeError("model has no complete/chat")
            except Exception as exc:  # noqa: BLE001
                return DeepPlan(notes=f"llm_planner_error:{type(exc).__name__}")

            body = str(text or "").strip()
            if body.startswith("```"):
                body = re.sub(r"^```(?:json)?\s*", "", body)
                body = re.sub(r"\s*```$", "", body)
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                # try extract first {...}
                m = re.search(r"\{.*\}", body, re.S)
                if not m:
                    return DeepPlan(notes="llm_planner_parse_error")
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return DeepPlan(notes="llm_planner_parse_error")
            if not isinstance(obj, dict):
                return DeepPlan(notes="llm_planner_bad_shape")
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
            allowed = set(allowed_keys)
            rerank_raw = obj.get("rerank_order")
            rerank: list[str] | None = None
            if isinstance(rerank_raw, list):
                rerank = [str(k) for k in rerank_raw if str(k) in allowed]
                if not rerank:
                    rerank = None
            return DeepPlan(
                subqueries=subs[:8],
                alias_extra=alias_extra[:16],
                rerank_order=rerank,
                notes="llm_planner",
            )

    return _LlmDeepPlanner()
