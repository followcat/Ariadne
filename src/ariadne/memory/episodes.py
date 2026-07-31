from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

EPISODE_EVENT_TYPES = {
    "problem",
    "goal",
    "hypothesis",
    "attempt",
    "observation",
    "error",
    "decision",
    "outcome",
    "preference_change",
    "workflow_signal",
    "entity_change",
}

TRAVERSAL_STEPS = {
    "resolve_entity",
    "follow_relation",
    "retrieve_timeline",
    "locate_decision",
    "locate_outcome",
    "expand_evidence",
}

_ASCII = re.compile(r"[a-z0-9_+.-]{2,}")
_CJK = re.compile(r"[\u4e00-\u9fff]")

EPISODE_SEARCH_WINDOW_RADIUS = 3
EPISODE_EXPAND_LIMIT_MAX = 16
EPISODE_EXPAND_BYTES_MAX = 16_000


def _tokens(text: str) -> set[str]:
    raw = (text or "").casefold()
    return set(_ASCII.findall(raw)) | set(_CJK.findall(raw))


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    session_id: str
    turn_id: str
    source: str
    quote: str
    tool_call_id: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True)
class EpisodeStore:
    """Locked, evidence-bound event episodes above the raw turn index."""

    path: Path
    max_episodes: int = 1024
    max_events_per_episode: int = 256

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, self._empty())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "episodes": {},
            "turn_index": {},
            "active_by_session": {},
            "seq": 0,
        }

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default=self._empty())
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_EPISODE_INVALID", "unknown episode store schema")
            )
        return data

    @staticmethod
    def _validate_event(event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        if event_type not in EPISODE_EVENT_TYPES:
            raise AriadneError(
                app_error(
                    "ARIADNE_EPISODE_INVALID",
                    f"unknown episode event type: {event_type!r}",
                )
            )
        content = str(event.get("content") or "").strip()
        if not content or len(content) > 2000:
            raise AriadneError(
                app_error(
                    "ARIADNE_EPISODE_INVALID",
                    "episode event content must be 1..2000 characters",
                )
            )
        evidence = event.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise AriadneError(
                app_error(
                    "ARIADNE_EPISODE_INVALID",
                    "episode events require at least one evidence reference",
                )
            )
        for ref in evidence:
            if not isinstance(ref, dict):
                raise AriadneError(
                    app_error("ARIADNE_EPISODE_INVALID", "invalid evidence reference")
                )
            if not str(ref.get("session_id") or "").strip() or not str(
                ref.get("turn_id") or ""
            ).strip():
                raise AriadneError(
                    app_error(
                        "ARIADNE_EPISODE_INVALID",
                        "evidence references require real session_id and turn_id",
                    )
                )

    def append_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        events: list[dict[str, Any]],
        title: str = "",
        close_episode: bool = False,
        ts: float | None = None,
        segment: str = "main",
    ) -> dict[str, Any]:
        sid = (session_id or "").strip()
        tid = (turn_id or "").strip()
        if not sid or not tid:
            raise AriadneError(
                app_error(
                    "ARIADNE_EPISODE_INVALID",
                    "episode append requires session_id and turn_id",
                )
            )
        seg = (segment or "main").strip() or "main"
        for event in events:
            self._validate_event(event)
            for ref in event.get("evidence") or []:
                if str(ref.get("session_id")) != sid or str(ref.get("turn_id")) != tid:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_EPISODE_INVALID",
                            "automatic episode events may cite only the appended turn",
                        )
                    )
        now = float(ts if ts is not None else time.time())
        session_key = f"{workspace_key or ''}\x1f{sid}"
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            turn_index = data.setdefault("turn_index", {})
            # Segment keys allow same-turn close A → open B without colliding.
            turn_key = f"{session_key}:{tid}#{seg}"
            legacy_key = f"{session_key}:{tid}"
            existing_id = turn_index.get(turn_key)
            if existing_id is None and seg == "main":
                existing_id = turn_index.get(legacy_key)
            episodes = data.setdefault("episodes", {})
            if existing_id:
                existing = episodes.get(existing_id)
                if existing is None:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_EPISODE_INVALID",
                            "episode turn index points to a missing episode",
                        )
                    )
                result.update(copy.deepcopy(existing))
                result["idempotent_replay"] = True
                return data

            active = data.setdefault("active_by_session", {})
            episode_id = str(active.get(session_key) or "")
            episode = episodes.get(episode_id) if episode_id else None
            if episode is None or episode.get("status") != "active":
                if len(episodes) >= self.max_episodes:
                    raise AriadneError(
                        app_error("ARIADNE_EPISODE_CAPACITY", "episode capacity exceeded")
                    )
                data["seq"] = int(data.get("seq") or 0) + 1
                episode_id = (
                    "ep-"
                    + hashlib.sha256(
                        f"{sid}:{tid}:{data['seq']}".encode("utf-8")
                    ).hexdigest()[:16]
                )
                episode = {
                    "episode_id": episode_id,
                    "title": (title or "").strip()[:200] or "Untitled episode",
                    "status": "active",
                    "session_id": sid,
                    "workspace_key": workspace_key or "",
                    "goal": "",
                    "observations": [],
                    "errors": [],
                    "attempts": [],
                    "decisions": [],
                    "outcomes": [],
                    "entities": [],
                    "related_turn_ids": [],
                    "events": [],
                    "created_at": now,
                    "updated_at": now,
                }
                episodes[episode_id] = episode
                active[session_key] = episode_id

            assert episode is not None
            if len(episode.get("events") or []) + len(events) > self.max_events_per_episode:
                raise AriadneError(
                    app_error(
                        "ARIADNE_EPISODE_CAPACITY",
                        "episode event capacity exceeded",
                        episode_id=episode_id,
                    )
                )
            related = episode.setdefault("related_turn_ids", [])
            if tid not in related:
                related.append(tid)
            for offset, raw in enumerate(events):
                event = copy.deepcopy(raw)
                event["event_id"] = event.get("event_id") or (
                    f"{episode_id}:e{len(episode['events']) + 1}"
                )
                event["ts"] = float(event.get("ts") or now + offset * 0.000001)
                event["session_id"] = sid
                event["turn_id"] = tid
                event.setdefault("entities", [])
                event.setdefault("metadata", {})
                episode["events"].append(event)
                content = str(event.get("content") or "")
                event_type = str(event.get("type") or "")
                if event_type == "goal" and not episode.get("goal"):
                    episode["goal"] = content
                    if episode.get("title") == "Untitled episode":
                        episode["title"] = content[:200]
                bucket = {
                    "observation": "observations",
                    "error": "errors",
                    "attempt": "attempts",
                    "decision": "decisions",
                    "outcome": "outcomes",
                }.get(event_type)
                if bucket:
                    episode.setdefault(bucket, []).append(event["event_id"])
                entities = episode.setdefault("entities", [])
                for entity in event.get("entities") or []:
                    entity_s = str(entity).strip()
                    if entity_s and entity_s not in entities:
                        entities.append(entity_s)
            episode["updated_at"] = now
            if close_episode:
                episode["status"] = "completed"
                active.pop(session_key, None)
            turn_index[turn_key] = episode_id
            # Keep a primary turn pointer on the latest segment for for_turn().
            turn_index[legacy_key] = episode_id
            result.update(copy.deepcopy(episode))
            result["idempotent_replay"] = False
            return data

        locked_update_json(self.path, mut, default=self._empty())
        return result

    @staticmethod
    def _visible_events(
        episode: dict[str, Any],
        *,
        allowed_turn_ids: set[str] | None,
        before_ts: float | None,
    ) -> list[dict[str, Any]]:
        visible: list[dict[str, Any]] = []
        for event in episode.get("events") or []:
            tid = str(event.get("turn_id") or "")
            if allowed_turn_ids is not None and tid not in allowed_turn_ids:
                continue
            if before_ts is not None and float(event.get("ts") or 0) >= before_ts:
                continue
            visible.append(event)
        return visible

    @staticmethod
    def _event_window(
        events: list[dict[str, Any]],
        anchors: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not events:
            return []
        anchor_ids = {
            str(event.get("event_id") or "") for event in anchors if event.get("event_id")
        }
        indices = [
            index
            for index, event in enumerate(events)
            if str(event.get("event_id") or "") in anchor_ids
        ]
        anchor = indices[-1] if indices else len(events) - 1
        start = max(0, anchor - EPISODE_SEARCH_WINDOW_RADIUS)
        stop = min(len(events), anchor + EPISODE_SEARCH_WINDOW_RADIUS + 1)
        return events[start:stop]

    @staticmethod
    def _hit(
        episode: dict[str, Any],
        events: list[dict[str, Any]],
        *,
        score: float,
        traversal_steps: list[str] | None = None,
        all_events: list[dict[str, Any]] | None = None,
        matched_events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if not events:
            return None
        visible_events = list(all_events or events)
        representative = events[-1]
        citations: list[dict[str, Any]] = []
        seen_refs: set[tuple[str, str, str, str]] = set()
        for event in events:
            for ref in event.get("evidence") or []:
                key = (
                    str(ref.get("session_id") or ""),
                    str(ref.get("turn_id") or ""),
                    str(ref.get("source") or ""),
                    str(ref.get("tool_call_id") or ""),
                )
                if not key[0] or not key[1] or key in seen_refs:
                    continue
                seen_refs.add(key)
                citations.append(copy.deepcopy(ref))
        chain = [
            {
                "event_id": event.get("event_id"),
                "type": event.get("type"),
                "content": event.get("content"),
                "reason": event.get("reason", ""),
                "entities": list(event.get("entities") or []),
                "relation": copy.deepcopy(event.get("relation")),
                "turn_id": event.get("turn_id"),
                "session_id": event.get("session_id"),
            }
            for event in events
        ]
        snippet = " -> ".join(
            f"{event.get('type')}: {event.get('content')}" for event in events[-6:]
        )[:800]
        return {
            "episode_id": episode.get("episode_id"),
            "turn_id": str(representative.get("turn_id") or ""),
            "session_id": str(
                representative.get("session_id") or episode.get("session_id") or ""
            ),
            "score": round(float(score), 4),
            "snippet": snippet,
            "kind": "episode",
            "related_turn_ids": list(
                dict.fromkeys(
                    str(event.get("turn_id") or "") for event in visible_events
                )
            ),
            "event_ids": [
                str(event.get("event_id") or "")
                for event in visible_events
                if event.get("event_id")
            ],
            "matched_event_ids": [
                str(event.get("event_id") or "")
                for event in (matched_events or events)
                if event.get("event_id")
            ],
            "event_chain": chain,
            "citations": citations,
            "traversal_steps": list(traversal_steps or []),
            "evidence": {
                "source": "episode",
                "episode_id": episode.get("episode_id"),
            },
            "evidence_page": {
                "returned_events": len(events),
                "total_events": len(visible_events),
                "has_more": len(events) < len(visible_events),
                "expand_tool": "memory_expand_evidence",
            },
        }

    def search(
        self,
        *,
        query: str,
        scope: str,
        session_id: str,
        workspace_key: str,
        limit: int = 8,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        q_tokens = _tokens(query)
        q_lower = (query or "").casefold()
        hits: list[dict[str, Any]] = []
        for episode in (self._read().get("episodes") or {}).values():
            if scope == "session" and episode.get("session_id") != session_id:
                continue
            if (
                scope == "session"
                and workspace_key
                and episode.get("workspace_key") != workspace_key
            ):
                continue
            if scope == "workspace" and episode.get("workspace_key") != workspace_key:
                continue
            events = self._visible_events(
                episode,
                allowed_turn_ids=(
                    allowed_turn_ids
                    if episode.get("session_id") == session_id
                    else None
                ),
                before_ts=before_ts,
            )
            matched: list[dict[str, Any]] = []
            score = 0.0
            for event in events:
                hay = " ".join(
                    [
                        str(event.get("content") or ""),
                        str(event.get("reason") or ""),
                        " ".join(str(x) for x in event.get("entities") or []),
                        str(event.get("relation") or ""),
                        str(event.get("metadata") or ""),
                    ]
                ).casefold()
                overlap = len(q_tokens & _tokens(hay))
                exact = bool(q_lower and q_lower in hay)
                if not overlap and not exact:
                    continue
                weight = 1.35 if event.get("type") in {"decision", "outcome"} else 1.0
                score += (overlap + (1.0 if exact else 0.0)) * weight
                matched.append(event)
            if not matched:
                continue
            window = self._event_window(events, matched)
            hit = self._hit(
                episode,
                window,
                score=score / max(len(q_tokens), 1),
                all_events=events,
                matched_events=matched,
            )
            if hit is not None:
                hits.append(hit)
        hits.sort(key=lambda row: -float(row.get("score") or 0))
        return hits[: max(1, limit)]

    def traverse(
        self,
        *,
        query: str,
        steps: list[str],
        scope: str,
        session_id: str,
        workspace_key: str,
        limit: int = 8,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        normalized = [str(step).strip() for step in steps if str(step).strip()]
        unknown = [step for step in normalized if step not in TRAVERSAL_STEPS]
        if unknown:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "deep memory plan contains unsupported traversal operations",
                    unknown=unknown,
                )
            )
        seed = self.search(
            query=query,
            scope=scope,
            session_id=session_id,
            workspace_key=workspace_key,
            limit=max(limit * 3, 8),
            allowed_turn_ids=allowed_turn_ids,
            before_ts=before_ts,
        )
        query_folded = (query or "").casefold()
        resolved: set[str] = set()
        for hit in seed:
            for event in hit.get("event_chain") or []:
                for entity in event.get("entities") or []:
                    entity_s = str(entity).strip()
                    if entity_s and entity_s.casefold() in query_folded:
                        resolved.add(entity_s)
        if not resolved and seed:
            # Deictic queries often omit the canonical entity. Ground the
            # resolution in seed-hit entities rather than model-generated ids.
            for event in seed[0].get("event_chain") or []:
                resolved.update(
                    str(entity).strip()
                    for entity in event.get("entities") or []
                    if str(entity).strip()
                )

        data = self._read()
        visible_episodes: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for episode in (data.get("episodes") or {}).values():
            if scope == "session" and episode.get("session_id") != session_id:
                continue
            if (
                scope == "session"
                and workspace_key
                and episode.get("workspace_key") != workspace_key
            ):
                continue
            if scope == "workspace" and episode.get("workspace_key") != workspace_key:
                continue
            events = self._visible_events(
                episode,
                allowed_turn_ids=(
                    allowed_turn_ids if episode.get("session_id") == session_id else None
                ),
                before_ts=before_ts,
            )
            if events:
                visible_episodes.append((episode, events))

        if "follow_relation" in normalized and resolved:
            # Bounded fixed-point expansion over stored relation endpoints.
            for _ in range(3):
                before = set(resolved)
                folded = {entity.casefold() for entity in resolved}
                for _episode, events in visible_episodes:
                    for event in events:
                        relation = event.get("relation")
                        if not isinstance(relation, dict):
                            continue
                        source = str(relation.get("from") or "").strip()
                        target = str(relation.get("to") or "").strip()
                        if source.casefold() in folded or target.casefold() in folded:
                            if source:
                                resolved.add(source)
                            if target:
                                resolved.add(target)
                if resolved == before:
                    break

        by_episode: dict[str, dict[str, Any]] = {}
        resolved_folded = {entity.casefold() for entity in resolved}
        for episode, events in visible_episodes:
            event_entities = {
                str(entity).strip().casefold()
                for event in events
                for entity in event.get("entities") or []
                if str(entity).strip()
            }
            relation_entities = {
                str(endpoint).strip().casefold()
                for event in events
                if isinstance(event.get("relation"), dict)
                for endpoint in (
                    event["relation"].get("from"),
                    event["relation"].get("to"),
                )
                if str(endpoint or "").strip()
            }
            if resolved_folded and not resolved_folded.intersection(
                event_entities | relation_entities
            ):
                continue
            if "locate_decision" in normalized and not any(
                event.get("type") == "decision" for event in events
            ):
                continue
            if "locate_outcome" in normalized and not any(
                event.get("type") == "outcome" for event in events
            ):
                continue
            if "follow_relation" in normalized and not any(
                event.get("relation") for event in events
            ) and not resolved_folded.intersection(event_entities):
                continue
            entity_score = len(resolved_folded.intersection(event_entities | relation_entities))
            token_score = len(_tokens(query) & _tokens(" ".join(
                str(event.get("content") or "") for event in events
            )))
            expanded_events = events
            if "retrieve_timeline" in normalized and resolved_folded:
                expanded_events = []
                for _related_episode, related_events in visible_episodes:
                    for related_event in related_events:
                        related_entities = {
                            str(entity).strip().casefold()
                            for entity in related_event.get("entities") or []
                            if str(entity).strip()
                        }
                        relation = related_event.get("relation")
                        if isinstance(relation, dict):
                            related_entities.update(
                                str(endpoint).strip().casefold()
                                for endpoint in (
                                    relation.get("from"),
                                    relation.get("to"),
                                )
                                if str(endpoint or "").strip()
                            )
                        if resolved_folded.intersection(related_entities):
                            expanded_events.append(related_event)
                expanded_events.sort(key=lambda event: float(event.get("ts") or 0))
                if not expanded_events:
                    expanded_events = events
            anchors = list(expanded_events)
            if "locate_outcome" in normalized:
                anchors = [
                    event for event in expanded_events if event.get("type") == "outcome"
                ]
            elif "locate_decision" in normalized:
                anchors = [
                    event for event in expanded_events if event.get("type") == "decision"
                ]
            window = self._event_window(expanded_events, anchors)
            hit = self._hit(
                episode,
                window,
                score=max(0.1, entity_score + token_score / max(len(_tokens(query)), 1)),
                traversal_steps=normalized,
                all_events=expanded_events,
                matched_events=anchors,
            )
            if hit is not None:
                by_episode[str(episode.get("episode_id") or "")] = hit

        out: list[dict[str, Any]] = []
        for hit in by_episode.values():
            hit["traversal_steps"] = normalized
            bonus = 0.04 * len(normalized)
            hit["score"] = round(float(hit.get("score") or 0) + bonus, 4)
            out.append(hit)
        out.sort(key=lambda row: -float(row.get("score") or 0))
        return out[: max(1, limit)]

    def expand_evidence(
        self,
        *,
        episode_id: str,
        scope: str,
        session_id: str,
        workspace_key: str,
        after_event_id: str = "",
        limit: int = 8,
        allowed_turn_ids: set[str] | None = None,
        before_ts: float | None = None,
    ) -> dict[str, Any]:
        if limit < 1 or limit > EPISODE_EXPAND_LIMIT_MAX:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"evidence page limit must be 1..{EPISODE_EXPAND_LIMIT_MAX}",
                )
            )
        episode = (self._read().get("episodes") or {}).get(episode_id)
        if not isinstance(episode, dict):
            raise AriadneError(
                app_error(
                    "ARIADNE_EPISODE_NOT_FOUND",
                    f"episode not found: {episode_id}",
                )
            )
        if scope == "session" and episode.get("session_id") != session_id:
            raise AriadneError(
                app_error("ARIADNE_EPISODE_NOT_FOUND", "episode is outside session scope")
            )
        if (
            scope == "workspace"
            and episode.get("workspace_key") != workspace_key
        ) or (
            scope == "session"
            and workspace_key
            and episode.get("workspace_key") != workspace_key
        ):
            raise AriadneError(
                app_error("ARIADNE_EPISODE_NOT_FOUND", "episode is outside workspace scope")
            )
        events = self._visible_events(
            episode,
            allowed_turn_ids=(
                allowed_turn_ids if episode.get("session_id") == session_id else None
            ),
            before_ts=before_ts,
        )
        start = 0
        cursor = (after_event_id or "").strip()
        if cursor:
            ids = [str(event.get("event_id") or "") for event in events]
            if cursor not in ids:
                raise AriadneError(
                    app_error(
                        "ARIADNE_EPISODE_EVENT_NOT_FOUND",
                        "evidence cursor does not identify a visible event",
                        episode_id=episode_id,
                        event_id=cursor,
                    )
                )
            start = ids.index(cursor) + 1

        page: list[dict[str, Any]] = []
        candidate_events = events[start : start + limit]
        for event in candidate_events:
            proposed = [*page, copy.deepcopy(event)]
            probe = {
                "episode_id": episode_id,
                "events": proposed,
                "has_more": start + len(proposed) < len(events),
                "next_after_event_id": str(proposed[-1].get("event_id") or ""),
                "max_bytes": EPISODE_EXPAND_BYTES_MAX,
                "returned_bytes": EPISODE_EXPAND_BYTES_MAX,
            }
            if len(
                json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            ) > EPISODE_EXPAND_BYTES_MAX:
                break
            page = proposed
        if candidate_events and not page:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_EVIDENCE_BUDGET",
                    "one episode event exceeds the evidence page byte budget",
                    episode_id=episode_id,
                    max_bytes=EPISODE_EXPAND_BYTES_MAX,
                )
            )
        has_more = start + len(page) < len(events)
        next_cursor = str(page[-1].get("event_id") or "") if page else cursor
        response = {
            "episode_id": episode_id,
            "events": page,
            "has_more": has_more,
            "next_after_event_id": next_cursor if has_more else "",
            "max_bytes": EPISODE_EXPAND_BYTES_MAX,
        }
        response["returned_bytes"] = 0
        for _ in range(2):
            response["returned_bytes"] = len(
                json.dumps(
                    response, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
        return response

    def list(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for episode in (self._read().get("episodes") or {}).values():
            if session_id is not None and episode.get("session_id") != session_id:
                continue
            rows.append(copy.deepcopy(episode))
        rows.sort(key=lambda row: float(row.get("created_at") or 0))
        return rows

    def for_turn(
        self, *, session_id: str, turn_id: str, workspace_key: str = ""
    ) -> dict[str, Any] | None:
        data = self._read()
        index = data.get("turn_index") or {}
        base = f"{workspace_key}\x1f{session_id}:{turn_id}"
        # Prefer the latest primary pointer (open segment overwrites close).
        episode_id = index.get(base)
        if not episode_id:
            episode_id = index.get(f"{base}#open") or index.get(f"{base}#main")
        if not episode_id:
            episode_id = index.get(f"{session_id}:{turn_id}")  # legacy v1 key
        episode = (data.get("episodes") or {}).get(episode_id) if episode_id else None
        return copy.deepcopy(episode) if episode is not None else None

    def for_turn_segment(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str = "",
        segment: str = "main",
    ) -> dict[str, Any] | None:
        data = self._read()
        index = data.get("turn_index") or {}
        base = f"{workspace_key}\x1f{session_id}:{turn_id}"
        seg = (segment or "main").strip() or "main"
        episode_id = index.get(f"{base}#{seg}")
        if not episode_id and seg == "main":
            episode_id = index.get(base)
        episode = (data.get("episodes") or {}).get(episode_id) if episode_id else None
        return copy.deepcopy(episode) if episode is not None else None
