"""Lightweight memory consolidation (personal Dream-style L3 promotion).

Turns explicit durable-looking *signals* into curated (L3) user entries under
**explicit apply** (default dry-run). Does not rewrite L2 conversation state.

Signals may come from:
- free-text lines (user messages, summaries)
- existing session-scoped curated entries (promote candidates)

Conservative heuristics only — no mandatory cloud LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .curated import CuratedStore

# Preference / durable-fact patterns (EN + common CN)
_SIGNAL_RE = re.compile(
    r"(?i)("
    r"\bprefer\b|"
    r"\balways\b|"
    r"\bnever\b|"
    r"\bdon'?t\b|"
    r"\bremember\b|"
    r"\bi (like|want|need|use)\b|"
    r"记住|偏好|总是|不要|请记住|我喜欢|我习惯"
    r")"
)

_MAX_CANDIDATE_CHARS = 400


@dataclass(slots=True)
class ConsolidationCandidate:
    content: str
    evidence: str
    source: str
    confidence: float


def _normalize_line(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def propose_from_texts(
    texts: list[str],
    *,
    source: str = "text",
    min_confidence: float = 0.55,
) -> list[ConsolidationCandidate]:
    """Extract durable-looking lines from raw texts (heuristic, no LLM)."""
    out: list[ConsolidationCandidate] = []
    seen: set[str] = set()
    for raw in texts:
        for line in str(raw or "").splitlines():
            clean = _normalize_line(line)
            if len(clean) < 8 or len(clean) > _MAX_CANDIDATE_CHARS:
                continue
            if clean.lower() in seen:
                continue
            conf = 0.0
            if _SIGNAL_RE.search(clean):
                conf = 0.7
            # Bullet / numbered preference style
            if re.match(r"^[-*•]\s+\S", clean) and _SIGNAL_RE.search(clean):
                conf = max(conf, 0.75)
            if conf < min_confidence:
                continue
            seen.add(clean.lower())
            out.append(
                ConsolidationCandidate(
                    content=clean.lstrip("-*• ").strip(),
                    evidence=clean[:200],
                    source=source,
                    confidence=conf,
                )
            )
    return out


def propose_from_session_curated(
    curated: CuratedStore,
    *,
    session_id: str,
    min_confidence: float = 0.6,
) -> list[ConsolidationCandidate]:
    """Session-scoped curated entries that look durable enough to promote to user."""
    data = curated.apply(action="read", scope="session", session_id=session_id)
    entries = data.get("entries") or []
    texts = [str(e.get("content") or "") for e in entries if isinstance(e, dict)]
    cands = propose_from_texts(texts, source="session_curated", min_confidence=min_confidence)
    # Session curated already explicit → slight confidence boost
    boosted: list[ConsolidationCandidate] = []
    for c in cands:
        boosted.append(
            ConsolidationCandidate(
                content=c.content,
                evidence=c.evidence,
                source=c.source,
                confidence=min(1.0, c.confidence + 0.1),
            )
        )
    return boosted


def _user_contents(curated: CuratedStore, *, session_id: str) -> set[str]:
    data = curated.apply(action="read", scope="user", session_id=session_id)
    return {
        str(e.get("content") or "").strip().lower()
        for e in (data.get("entries") or [])
        if isinstance(e, dict)
    }


def consolidate(
    curated: CuratedStore,
    *,
    session_id: str,
    texts: list[str] | None = None,
    include_session_curated: bool = True,
    apply: bool = False,
    scope: str = "user",
    min_confidence: float = 0.55,
) -> dict[str, Any]:
    """Propose and optionally apply L3 curated adds.

    Default ``apply=False`` is dry-run (list candidates only). Never silently
    rewrites L2 state.
    """
    scope = (scope or "user").strip().lower()
    if scope not in {"user", "session"}:
        scope = "user"
    candidates: list[ConsolidationCandidate] = []
    if texts:
        candidates.extend(
            propose_from_texts(texts, source="text", min_confidence=min_confidence)
        )
    if include_session_curated:
        candidates.extend(
            propose_from_session_curated(
                curated, session_id=session_id, min_confidence=min_confidence
            )
        )
    # Dedupe by content
    by_key: dict[str, ConsolidationCandidate] = {}
    for c in candidates:
        k = c.content.lower()
        if k not in by_key or c.confidence > by_key[k].confidence:
            by_key[k] = c
    candidates = sorted(by_key.values(), key=lambda x: -x.confidence)

    existing = _user_contents(curated, session_id=session_id) if scope == "user" else set()
    # also skip if already in target scope session
    if scope == "session":
        data = curated.apply(action="read", scope="session", session_id=session_id)
        existing = {
            str(e.get("content") or "").strip().lower()
            for e in (data.get("entries") or [])
            if isinstance(e, dict)
        }

    planned: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for c in candidates:
        row = {
            "content": c.content,
            "evidence": c.evidence,
            "source": c.source,
            "confidence": round(c.confidence, 3),
        }
        if c.content.lower() in existing:
            skipped.append({**row, "reason": "already_present"})
            continue
        planned.append(row)
        if not apply:
            continue
        try:
            curated.apply(
                action="add",
                content=c.content,
                scope=scope,
                session_id=session_id,
            )
            existing.add(c.content.lower())
            applied.append(row)
        except Exception as exc:  # caps full, etc. — report, do not silent-pass as success
            skipped.append({**row, "reason": f"apply_failed:{exc}"})

    return {
        "apply": apply,
        "scope": scope,
        "session_id": session_id,
        "candidates": planned if not apply else applied + [p for p in planned if p not in applied],
        "proposed_count": len(planned),
        "applied_count": len(applied),
        "skipped": skipped,
        "message": (
            f"consolidation applied {len(applied)} entries"
            if apply
            else f"consolidation dry-run: {len(planned)} candidates (pass apply=True to write)"
        ),
    }
