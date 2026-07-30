from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .episodes import EPISODE_EVENT_TYPES, EpisodeStore, EvidenceRef
from .prospective import ProspectiveMemoryStore
from .reflection import ReflectionStore
from .state import ConversationStateStore
from .user_model import UserModelStore

MemoryExtractorFn = Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]

_AMBIGUOUS = re.compile(
    r"(还是按之前|改成前者|改成后者|那个(?:方案|偏好|设置)|这和上次|不对[，, ]*改成|"
    r"same as before|the former|the latter|that (?:setting|preference|approach))",
    re.I,
)

_PREFERENCE_RE = re.compile(
    r"以后\s*(?P<context>[^，。,.]{0,48}?)\s*都用\s*"
    r"(?P<new>[A-Za-z0-9_.+\-/]+)\s*[，, ]*"
    r"(?:不用|不要|而不是)\s*(?P<old>[A-Za-z0-9_.+\-/]+)",
    re.I,
)
_PROSPECTIVE_PATH_RE = re.compile(
    r"下次(?:修改|改动|碰到)\s*(?P<path>[^，,。\s]+)\s*时[，,\s]*"
    r"(?:提醒(?:我)?|记得)\s*(?P<content>[^。]+)",
    re.I,
)
_PROSPECTIVE_RELEASE_RE = re.compile(
    r"发布前\s*(?:提醒(?:我)?|记得)\s*(?P<content>[^。]+)", re.I
)


def _evidence(
    *,
    session_id: str,
    turn_id: str,
    source: str,
    quote: str,
    tool_call_id: str = "",
) -> list[dict[str, str]]:
    return [
        EvidenceRef(
            session_id=session_id,
            turn_id=turn_id,
            source=source,
            quote=quote[:800],
            tool_call_id=tool_call_id,
        ).to_dict()
    ]


def _event(
    event_type: str,
    content: str,
    *,
    session_id: str,
    turn_id: str,
    source: str,
    quote: str,
    entities: list[str] | None = None,
    reason: str = "",
    relation: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
    tool_call_id: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": event_type,
        "content": content.strip()[:2000],
        "reason": reason.strip()[:1000],
        "entities": list(entities or []),
        "metadata": dict(metadata or {}),
        "evidence": _evidence(
            session_id=session_id,
            turn_id=turn_id,
            source=source,
            quote=quote,
            tool_call_id=tool_call_id,
        ),
    }
    if relation:
        row["relation"] = dict(relation)
    return row


def _entities(text: str) -> list[str]:
    out: list[str] = []
    for value in re.findall(r"`([^`]{2,80})`", text or ""):
        if value not in out:
            out.append(value)
    known = re.findall(
        r"\b(?:Ariadne|Redis|SQLite|Python|GitHub|Neovim|Vim|VS\s*Code|uv|poetry)\b",
        text or "",
        re.I,
    )
    for value in known:
        clean = value.strip()
        if clean and clean not in out:
            out.append(clean)
    for value in re.findall(r"(?:转给|交给|负责人(?:是|改成))\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})", text or ""):
        if value not in out:
            out.append(value)
    return out[:24]


def _preference_key(context: str, new_value: str, old_value: str) -> str:
    hay = f"{context} {new_value} {old_value}".casefold()
    if "python" in hay and {new_value.casefold(), old_value.casefold()} & {"uv", "poetry", "pip"}:
        return "python_package_manager"
    slug = re.sub(r"[^a-z0-9]+", "_", context.casefold()).strip("_")
    return (slug[:96] + "_preference") if slug else "tool_preference"


def _reason_from(text: str) -> str:
    match = re.search(r"(?:因为|原因是|because)\s*[:：]?\s*([^。\n]+)", text or "", re.I)
    return match.group(1).strip()[:1000] if match else ""


def _dedupe(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        key = (
            str(event.get("type") or ""),
            str(event.get("content") or "").strip().casefold(),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(event)
    return out


def _deterministic_events(
    *, session_id: str, turn_id: str, user_text: str, assistant_text: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    user_entities = _entities(user_text)
    assistant_entities = _entities(assistant_text)

    preference = _PREFERENCE_RE.search(user_text)
    if preference:
        context = preference.group("context").strip()
        new_value = preference.group("new").strip()
        old_value = preference.group("old").strip()
        events.append(
            _event(
                "preference_change",
                f"{context or 'default'}: {old_value} -> {new_value}",
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=preference.group(0),
                entities=user_entities,
                metadata={
                    "entry_type": "preference",
                    "key": _preference_key(context, new_value, old_value),
                    "value": new_value,
                    "previous_value": old_value,
                    "scope": "user",
                    "explicit_durable": True,
                    "evidence_quote": preference.group(0),
                },
            )
        )

    workflow_patterns = (
        (
            r"(?:review|代码审查)[^。\n]{0,30}(?:先看|先检查)(?:测试|test)",
            "review_order",
            "tests_first",
        ),
        (
            r"(?:不要|避免|不喜欢)[^。\n]{0,18}(?:大范围|大规模)(?:重构|改动)",
            "change_scope",
            "small_explicit_changes",
        ),
        (r"(?:更重视|优先|坚持)[^。\n]{0,10}fastfail", "error_policy", "fastfail"),
    )
    for pattern, key, value in workflow_patterns:
        match = re.search(pattern, user_text, re.I)
        if match:
            quote = match.group(0)
            events.append(
                _event(
                    "workflow_signal",
                    f"{key}={value}",
                    session_id=session_id,
                    turn_id=turn_id,
                    source="user",
                    quote=quote,
                    entities=user_entities,
                    metadata={
                        "entry_type": "preference",
                        "key": key,
                        "value": value,
                        "scope": "user",
                        "explicit_durable": False,
                        "evidence_quote": quote,
                    },
                )
            )

    # Requests and explicit targets become episode goals, not durable profile facts.
    goal_match = re.search(
        r"(?:目标是\s*([^。\n]{2,500})|^\s*(?:请|帮我|需要|希望)\s*([^。\n]{2,500}))",
        user_text,
        re.I,
    )
    if goal_match:
        goal_content = (goal_match.group(1) or goal_match.group(2) or "").strip()
        events.append(
            _event(
                "goal",
                goal_content,
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=goal_match.group(0),
                entities=user_entities,
            )
        )
    problem_match = re.search(
        r"([^。\n]{0,180}(?:问题|故障|报错|bug|error|失败)[^。\n]{0,180})",
        user_text,
        re.I,
    )
    if problem_match:
        events.append(
            _event(
                "problem",
                problem_match.group(1).strip(),
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=problem_match.group(1),
                entities=user_entities,
            )
        )
    hypothesis_match = re.search(
        r"([^。\n]{0,120}(?:可能|怀疑|推测|猜测|maybe|suspect)[^。\n]{0,180})",
        user_text,
        re.I,
    )
    if hypothesis_match:
        events.append(
            _event(
                "hypothesis",
                hypothesis_match.group(1).strip(),
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=hypothesis_match.group(1),
                entities=user_entities,
            )
        )

    combined_candidates = (("user", user_text, user_entities), ("assistant", assistant_text, assistant_entities))
    for source, text, entities in combined_candidates:
        for event_type, pattern in (
            ("attempt", r"([^。\n]{0,160}(?:尝试|试了|改为|修改了|ran|tried)[^。\n]{0,220})"),
            ("observation", r"([^。\n]{0,160}(?:发现|观察到|结果(?:是|显示)?|报错|shows?)[^。\n]{0,220})"),
            ("decision", r"([^。\n]{0,160}(?:决定|选择|采用|不采用|放弃|decided|rejected)[^。\n]{0,260})"),
            ("outcome", r"([^。\n]{0,160}(?:已解决|修复完成|完成了|测试通过|通过了|仍失败|没有解决|resolved|passed)[^。\n]{0,220})"),
        ):
            match = re.search(pattern, text, re.I)
            if not match:
                continue
            quote = match.group(1).strip()
            events.append(
                _event(
                    event_type,
                    quote,
                    session_id=session_id,
                    turn_id=turn_id,
                    source=source,
                    quote=quote,
                    entities=entities,
                    reason=_reason_from(quote) if event_type == "decision" else "",
                )
            )

    assignment = re.search(r"([^。\n]{2,80})[^。\n]{0,40}(?:转给|交给)\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})", user_text)
    if assignment:
        subject = assignment.group(1).strip()
        assignee = assignment.group(2).strip()
        quote = assignment.group(0)
        events.append(
            _event(
                "entity_change",
                f"{subject} assigned_to {assignee}",
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=quote,
                entities=[subject, assignee],
                relation={"type": "assigned_to", "from": subject, "to": assignee},
            )
        )
    return _dedupe(events)


def _tool_value(tool: Any, name: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(name, default)
    return getattr(tool, name, default)


def _tool_events(
    *, session_id: str, turn_id: str, tool_calls: list[Any]
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for call in tool_calls:
        name = str(_tool_value(call, "name", "") or "").strip()
        if not name:
            continue
        call_id = str(_tool_value(call, "call_id", "") or "")
        status = str(_tool_value(call, "status", "") or "")
        arguments = _tool_value(call, "arguments", {}) or {}
        output = _tool_value(call, "output", None)
        attempt_quote = f"{name}: {json.dumps(arguments, ensure_ascii=False, default=str)[:600]}"
        events.append(
            _event(
                "attempt",
                f"called {name}",
                session_id=session_id,
                turn_id=turn_id,
                source="tool",
                quote=attempt_quote,
                metadata={"tool_name": name, "arguments": arguments},
                tool_call_id=call_id,
            )
        )
        output_quote = json.dumps(output, ensure_ascii=False, default=str)[:800]
        events.append(
            _event(
                "observation" if status == "completed" else "outcome",
                f"{name} {status}: {output_quote}",
                session_id=session_id,
                turn_id=turn_id,
                source="tool",
                quote=output_quote or status,
                metadata={"tool_name": name, "status": status},
                tool_call_id=call_id,
            )
        )
    return events


def _prospective_specs(user_text: str) -> list[tuple[str, dict[str, Any]]]:
    specs: list[tuple[str, dict[str, Any]]] = []
    for match in _PROSPECTIVE_PATH_RE.finditer(user_text or ""):
        specs.append(
            (
                match.group("content").strip(),
                {"path_glob": match.group("path").strip()},
            )
        )
    for match in _PROSPECTIVE_RELEASE_RE.finditer(user_text or ""):
        specs.append(
            (
                match.group("content").strip(),
                {"text_contains": ["发布", "release"]},
            )
        )
    return specs


def _collect_paths(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, dict):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, list):
            for child in item:
                visit(child, key)
        elif isinstance(item, str) and (
            "path" in key.casefold() or "file" in key.casefold()
        ):
            clean = item.strip()
            if clean:
                found.append(clean)
                if clean.startswith("/workspace/"):
                    found.append(clean[len("/workspace/") :])

    visit(value)
    return list(dict.fromkeys(found))


@dataclass(slots=True)
class AutomaticMemoryProjector:
    episodes: EpisodeStore
    user_model: UserModelStore
    state: ConversationStateStore | None = None
    reflection: ReflectionStore | None = None
    prospective: ProspectiveMemoryStore | None = None
    extractor: MemoryExtractorFn | None = None

    async def capture_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        user_text: str,
        assistant_text: str,
        tool_calls: list[Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.episodes.for_turn(
            session_id=session_id,
            turn_id=turn_id,
            workspace_key=workspace_key,
        )
        if existing is not None:
            return {
                "status": "used" if existing.get("events") else "skipped",
                "episode_id": existing.get("episode_id"),
                "event_ids": [
                    str(event.get("event_id") or "")
                    for event in existing.get("events") or []
                    if event.get("turn_id") == turn_id
                ],
                "user_model_entry_ids": [],
                "reflection_candidate_ids": [],
                "prospective_entry_ids": [],
                "triggered_prospective_ids": [],
                "llm_used": False,
                "llm_rejected": 0,
                "idempotent_replay": True,
            }
        events = _deterministic_events(
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
        )
        events.extend(
            _tool_events(
                session_id=session_id,
                turn_id=turn_id,
                tool_calls=list(tool_calls or []),
            )
        )
        llm_used = False
        llm_rejected = 0
        if self.extractor is not None and _AMBIGUOUS.search(user_text or ""):
            llm_used = True
            proposed = await self.extractor(
                {
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "user_text": user_text,
                    "assistant_text": assistant_text,
                    "tool_evidence": [
                        {
                            "name": _tool_value(call, "name", ""),
                            "arguments": _tool_value(call, "arguments", {}),
                            "output": _tool_value(call, "output", None),
                            "status": _tool_value(call, "status", ""),
                        }
                        for call in tool_calls or []
                    ],
                    "current_user_model": [
                        {
                            "type": row.get("type"),
                            "key": row.get("key"),
                            "value": row.get("value"),
                            "scope": row.get("scope"),
                        }
                        for row in self.user_model.list(
                            workspace_key=workspace_key,
                            session_id=session_id,
                        )
                    ],
                }
            )
            evidence_text = "\n".join([user_text, assistant_text])
            for row in proposed:
                event_type = str(row.get("type") or "")
                quote = str(row.get("evidence_quote") or "").strip()
                content = str(row.get("content") or "").strip()
                if (
                    event_type not in EPISODE_EVENT_TYPES
                    or not quote
                    or quote not in evidence_text
                    or not content
                ):
                    llm_rejected += 1
                    continue
                metadata = (
                    dict(row.get("metadata"))
                    if isinstance(row.get("metadata"), dict)
                    else {}
                )
                # An ambiguity model may identify a pattern/change candidate,
                # but it cannot grant itself durable-write authority.
                metadata["explicit_durable"] = False
                events.append(
                    _event(
                        event_type,
                        content,
                        session_id=session_id,
                        turn_id=turn_id,
                        source=("user" if quote in user_text else "assistant"),
                        quote=quote,
                        entities=[str(item) for item in row.get("entities") or []],
                        reason=str(row.get("reason") or ""),
                        relation=(
                            dict(row.get("relation"))
                            if isinstance(row.get("relation"), dict)
                            else None
                        ),
                        metadata=metadata,
                    )
                )
        events = _dedupe(events)

        user_model_ids: list[str] = []
        reflection_signals: list[dict[str, Any]] = []
        for event in events:
            metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
            if event.get("type") in {"preference_change", "workflow_signal"}:
                signal = {
                    "entry_type": metadata.get("entry_type") or "preference",
                    "key": metadata.get("key"),
                    "value": metadata.get("value"),
                    "scope": metadata.get("scope") or "user",
                    "explicit_durable": bool(metadata.get("explicit_durable")),
                    "evidence_quote": metadata.get("evidence_quote")
                    or ((event.get("evidence") or [{}])[0].get("quote")),
                }
                reflection_signals.append(signal)
                if signal["explicit_durable"] and signal["key"]:
                    evidence = list(event.get("evidence") or [])
                    row = self.user_model.upsert_by_key(
                        entry_type=str(signal["entry_type"]),
                        key=str(signal["key"]),
                        value=signal["value"],
                        source="user_explicit",
                        confidence=1.0,
                        scope=str(signal["scope"]),
                        workspace_key=(
                            workspace_key if signal["scope"] == "workspace" else ""
                        ),
                        session_id=(session_id if signal["scope"] == "session" else ""),
                        source_turn_id=turn_id,
                        change_reason=str(metadata.get("change_reason") or event.get("reason") or "explicit user change"),
                        evidence=evidence,
                    )
                    user_model_ids.append(str(row.get("entry_id") or ""))

        state_version: int | None = None
        if self.state is not None:
            state_ops: list[dict[str, Any]] = []
            goal = next((event for event in events if event.get("type") == "goal"), None)
            if goal is not None:
                quote = str(((goal.get("evidence") or [{}])[0]).get("quote") or "")
                state_ops.extend(
                    [
                        {
                            "op": "ensure_entity",
                            "entity_id": "session:current_goal",
                            "type": "goal",
                            "evidence_quote": quote,
                        },
                        {
                            "op": "set_attribute",
                            "entity_id": "session:current_goal",
                            "key": "description",
                            "value": goal.get("content"),
                            "memory_type": "goal",
                            "authority": "user_explicit",
                            "evidence_quote": quote,
                        },
                        {
                            "op": "set_status",
                            "entity_id": "session:current_goal",
                            "status": "active",
                            "evidence_quote": quote,
                        },
                    ]
                )
            completed_outcome = next(
                (
                    event
                    for event in events
                    if event.get("type") == "outcome"
                    and re.search(
                        r"已解决|修复完成|测试通过|通过了|resolved|passed",
                        str(event.get("content") or ""),
                        re.I,
                    )
                    and not re.search(
                        r"失败|没有解决|仍失败|failed|unresolved",
                        str(event.get("content") or ""),
                        re.I,
                    )
                ),
                None,
            )
            current_state = self.state.get(session_id)
            has_current_goal = "session:current_goal" in (
                current_state.get("entities") or {}
            )
            if completed_outcome is not None and (goal is not None or has_current_goal):
                quote = str(
                    ((completed_outcome.get("evidence") or [{}])[0]).get("quote")
                    or ""
                )
                state_ops.append(
                    {
                        "op": "set_status",
                        "entity_id": "session:current_goal",
                        "status": "done",
                        "evidence_quote": quote,
                    }
                )
            if state_ops:
                evidence_text = "\n".join(
                    str(ref.get("quote") or "")
                    for event in events
                    for ref in event.get("evidence") or []
                )
                state_result = self.state.apply_ops(
                    session_id=session_id,
                    operations=state_ops,
                    source_turn_id=turn_id,
                    evidence_text=evidence_text,
                    idempotency_key=f"auto_capture:{turn_id}",
                )
                state_version = int(state_result.get("version") or 0)

        title_event = next(
            (event for event in events if event.get("type") in {"goal", "problem"}),
            None,
        )
        episode = self.episodes.append_turn(
            session_id=session_id,
            turn_id=turn_id,
            workspace_key=workspace_key,
            events=events,
            title=str((title_event or {}).get("content") or "")[:200],
            close_episode=any(event.get("type") == "outcome" for event in events),
        )

        reflection_ids: list[str] = []
        if self.reflection is not None and reflection_signals:
            candidates = self.reflection.observe(
                session_id=session_id,
                turn_id=turn_id,
                signals=reflection_signals,
            )
            reflection_ids = [str(row.get("candidate_id") or "") for row in candidates]

        prospective_ids: list[str] = []
        triggered_ids: list[str] = []
        if self.prospective is not None:
            tool_names = [
                str(_tool_value(call, "name", "") or "") for call in tool_calls or []
            ]
            changed_paths: list[str] = []
            for call in tool_calls or []:
                changed_paths.extend(_collect_paths(_tool_value(call, "arguments", {})))
                changed_paths.extend(_collect_paths(_tool_value(call, "output", {})))
            triggered = self.prospective.match(
                context={
                    "workspace": workspace_key,
                    "text": "\n".join([user_text, assistant_text]),
                    "changed_paths": list(dict.fromkeys(changed_paths)),
                    "tool_names": tool_names,
                    "event_types": [str(event.get("type") or "") for event in events],
                    "entity_ids": list(
                        dict.fromkeys(
                            str(entity)
                            for event in events
                            for entity in event.get("entities") or []
                        )
                    ),
                }
            )
            triggered_ids = [str(row.get("entry_id") or "") for row in triggered]
            # Create new "next time" records only after matching existing ones,
            # so their own source sentence cannot trigger them immediately.
            for index, (content, trigger) in enumerate(_prospective_specs(user_text)):
                row = self.prospective.create(
                    content=content,
                    trigger=trigger,
                    source_session_id=session_id,
                    source_turn_id=turn_id,
                    idempotency_key=f"auto:{session_id}:{turn_id}:{index}",
                )
                prospective_ids.append(str(row.get("entry_id") or ""))

        return {
            "status": "used" if events or prospective_ids else "skipped",
            "episode_id": episode.get("episode_id"),
            "event_ids": [str(event.get("event_id") or "") for event in episode.get("events") or [] if event.get("turn_id") == turn_id],
            "user_model_entry_ids": user_model_ids,
            "state_version": state_version,
            "reflection_candidate_ids": reflection_ids,
            "prospective_entry_ids": prospective_ids,
            "triggered_prospective_ids": triggered_ids,
            "llm_used": llm_used,
            "llm_rejected": llm_rejected,
        }


def make_llm_memory_extractor(model: Any) -> MemoryExtractorFn:
    """Create an ambiguity-only, evidence-quoting structured extractor."""

    async def extract(payload: dict[str, Any]) -> list[dict[str, Any]]:
        system = (
            "Extract memory events only from the supplied completed turn. Reply JSON only as "
            '{"events":[{"type":"...","content":"...","evidence_quote":"verbatim",'
            '"entities":[],"reason":"","relation":null,"metadata":{}}]}. '
            "Allowed types: problem, goal, hypothesis, attempt, observation, decision, outcome, "
            "preference_change, workflow_signal, entity_change. evidence_quote must be verbatim. "
            "Do not invent facts, ids, prior conversation, or durable preferences. Return [] when uncertain."
        )
        user = json.dumps(payload, ensure_ascii=False, default=str)[:12000]
        exchange = await model.complete(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            tools=None,
            tool_choice=None,
            temperature=0.0,
            max_tokens=1200,
        )
        body = str(getattr(getattr(exchange, "message", None), "content", "") or "").strip()
        if body.startswith("```"):
            body = re.sub(r"^```(?:json)?\s*", "", body)
            body = re.sub(r"\s*```$", "", body)
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", body, re.S)
            if not match:
                return []
            try:
                obj = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []
        events = obj.get("events") if isinstance(obj, dict) else None
        return [dict(row) for row in events or [] if isinstance(row, dict)][:32]

    return extract
