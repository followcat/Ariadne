from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..errors import AriadneError, app_error
from ..redact import redact_secrets, redact_text
from .capture_journal import CaptureJournalStore
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
            quote=redact_text(quote)[:800],
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
        "content": redact_text(content.strip())[:2000],
        "reason": redact_text(reason.strip())[:1000],
        "entities": [redact_text(str(item))[:200] for item in (entities or [])],
        "metadata": redact_secrets(dict(metadata or {})),
        "evidence": _evidence(
            session_id=session_id,
            turn_id=turn_id,
            source=source,
            quote=quote,
            tool_call_id=tool_call_id,
        ),
    }
    if relation:
        row["relation"] = redact_secrets(dict(relation))
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


_COMPLETION_RE = re.compile(
    r"已解决|修复完成|完成了|测试通过|通过了|resolved|passed|completed", re.I
)
_NONTERMINAL_RE = re.compile(
    r"仍失败|没有解决|未解决|失败|failed|unresolved|还需|继续尝试", re.I
)
_ABANDON_RE = re.compile(
    r"放弃(?:这个|该)?(?:任务|目标|问题|工作)|停止处理|不再处理", re.I
)
_CANCEL_RE = re.compile(
    r"取消(?:这个|该)?(?:任务|目标|工作)|不做了|cancel(?:led)?", re.I
)
# Terminal state is an authority boundary, not a text-classification result.
# The current closed path is Task verifier evidence. A future exact
# user-confirmation contract may add another authority here, but free text and
# ordinary tool output must never become terminal merely by matching keywords.
_TERMINAL_AUTHORITIES = {"verified_check"}


def _lifecycle_metadata(*, event_type: str, text: str, source: str) -> dict[str, Any]:
    authority = {
        "user": "user_explicit",
        "tool": "tool_observed",
        "verifier": "verified_check",
    }.get(source, "model_assertion")
    metadata: dict[str, Any] = {
        "authority": authority,
        "terminal": False,
    }
    if event_type == "decision" and _ABANDON_RE.search(text):
        metadata["outcome_kind"] = "abandoned"
    elif event_type == "outcome":
        if _CANCEL_RE.search(text):
            metadata["outcome_kind"] = "cancelled"
        elif _ABANDON_RE.search(text):
            metadata["outcome_kind"] = "abandoned"
        elif _NONTERMINAL_RE.search(text):
            metadata["outcome_kind"] = "nonterminal"
        elif _COMPLETION_RE.search(text):
            metadata["outcome_kind"] = "completed"
        else:
            metadata["outcome_kind"] = "nonterminal"
    return metadata


def _is_terminal_event(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get("terminal")) and str(
        metadata.get("authority") or ""
    ) in _TERMINAL_AUTHORITIES


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

    combined_candidates = (
        ("user", user_text, user_entities),
        ("assistant", assistant_text, assistant_entities),
    )
    for source, text, entities in combined_candidates:
        for event_type, pattern in (
            ("attempt", r"([^。\n]{0,160}(?:尝试|试了|改为|修改了|ran|tried)[^。\n]{0,220})"),
            ("observation", r"([^。\n]{0,160}(?:发现|观察到|结果(?:是|显示)?|报错|shows?)[^。\n]{0,220})"),
            ("decision", r"([^。\n]{0,160}(?:决定|选择|采用|不采用|放弃|decided|rejected)[^。\n]{0,260})"),
            (
                "outcome",
                r"([^。\n]{0,160}(?:已解决|修复完成|完成了|测试通过|通过了|"
                r"仍失败|没有解决|取消(?:这个|该)?(?:任务|目标|工作)|不做了|"
                r"停止处理|resolved|passed|cancelled)[^。\n]{0,220})",
            ),
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
                    metadata=_lifecycle_metadata(
                        event_type=event_type,
                        text=quote,
                        source=source,
                    ),
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


def _payload_digest(value: Any) -> str:
    safe = redact_secrets(value)
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_tool_summary(value: Any) -> str:
    safe = redact_secrets(value)
    if isinstance(safe, dict):
        allow = {
            "status",
            "ok",
            "success",
            "exit_code",
            "returncode",
            "count",
            "path",
            "changed",
            "passed",
            "failed",
        }
        summary: dict[str, Any] = {}
        for key, item in safe.items():
            key_text = str(key)
            if key_text not in allow:
                continue
            if isinstance(item, (dict, list, tuple)):
                summary[key_text] = "structured value retained by digest only"
            elif isinstance(item, str):
                summary[key_text] = (
                    item
                    if len(item) <= 160
                    else f"text chars={len(item)} retained by digest only"
                )
            elif isinstance(item, bool) or item is None:
                summary[key_text] = item
            elif isinstance(item, int):
                summary[key_text] = (
                    item
                    if len(str(item)) <= 64
                    else "numeric value retained by digest only"
                )
            elif isinstance(item, float) and math.isfinite(item):
                summary[key_text] = item
            else:
                summary[key_text] = "value retained by digest only"
        if isinstance(value, dict) and "error" in value:
            summary["has_error"] = value.get("error") not in (None, "", False)
        if not summary:
            return "structured output retained by digest only"
        safe = summary
    elif isinstance(safe, list):
        return f"list output count={len(safe)} retained by digest only"
    elif isinstance(safe, str):
        return f"text output chars={len(safe)} retained by digest only"
    return redact_text(json.dumps(safe, ensure_ascii=False, default=str))[:400]


def _sanitized_tool_evidence(tool_calls: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call in tool_calls:
        arguments = _tool_value(call, "arguments", {}) or {}
        output = _tool_value(call, "output", None)
        rows.append(
            {
                "name": str(_tool_value(call, "name", "") or ""),
                "status": str(_tool_value(call, "status", "") or ""),
                "arguments_digest": _payload_digest(arguments),
                "argument_keys": (
                    sorted(str(key) for key in arguments)
                    if isinstance(arguments, dict)
                    else []
                ),
                "output_digest": _payload_digest(output),
                "output_summary": _safe_tool_summary(output),
            }
        )
    return rows


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
        arguments_digest = _payload_digest(arguments)
        output_digest = _payload_digest(output)
        attempt_quote = f"{name} arguments_sha256={arguments_digest}"
        events.append(
            _event(
                "attempt",
                f"called {name}",
                session_id=session_id,
                turn_id=turn_id,
                source="tool",
                quote=attempt_quote,
                metadata={
                    "tool_name": name,
                    "arguments_sha256": arguments_digest,
                    "argument_keys": (
                        sorted(str(key) for key in arguments)
                        if isinstance(arguments, dict)
                        else []
                    ),
                    "authority": "tool_observed",
                    "terminal": False,
                },
                tool_call_id=call_id,
            )
        )
        output_quote = _safe_tool_summary(output) or status
        events.append(
            _event(
                "observation" if status == "completed" else "error",
                f"{name} {status}: {output_quote}",
                session_id=session_id,
                turn_id=turn_id,
                source="tool",
                quote=output_quote,
                metadata={
                    "tool_name": name,
                    "status": status,
                    "output_sha256": output_digest,
                    "authority": "tool_observed",
                    "terminal": False,
                },
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
    journal: CaptureJournalStore
    state: ConversationStateStore | None = None
    reflection: ReflectionStore | None = None
    prospective: ProspectiveMemoryStore | None = None
    extractor: MemoryExtractorFn | None = None
    resume_batch_size: int = 4

    async def _prepare(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        user_text: str,
        assistant_text: str,
        tool_calls: list[Any],
        verified_goal: dict[str, Any] | None,
    ) -> dict[str, Any]:
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
                tool_calls=tool_calls,
            )
        )
        verified = redact_secrets(dict(verified_goal or {}))
        if verified.get("status") == "completed":
            content = str(
                verified.get("summary")
                or verified.get("goal")
                or "task goal checks passed"
            ).strip()
            if content:
                events.append(
                    _event(
                        "outcome",
                        content,
                        session_id=session_id,
                        turn_id=turn_id,
                        source="verifier",
                        quote=content,
                        entities=_entities(content),
                        metadata={
                            "authority": "verified_check",
                            "terminal": True,
                            "outcome_kind": "verified_completion",
                            "task_id": str(verified.get("task_id") or ""),
                            "check_ids": [
                                str(item) for item in verified.get("check_ids") or []
                            ],
                        },
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
                    "user_text": redact_text(user_text),
                    "assistant_text": redact_text(assistant_text),
                    "tool_evidence": _sanitized_tool_evidence(tool_calls),
                    "current_user_model": [
                        {
                            "type": row.get("type"),
                            "key": row.get("key"),
                            "value": redact_secrets(row.get("value")),
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
                if not quote or quote not in evidence_text:
                    llm_rejected += 1
                    continue
                metadata = dict(row.get("metadata") or {})
                metadata["explicit_durable"] = False
                source = "user" if quote in user_text else "assistant"
                metadata.update(
                    _lifecycle_metadata(
                        event_type=event_type,
                        text=content,
                        source=source,
                    )
                )
                events.append(
                    _event(
                        event_type,
                        content,
                        session_id=session_id,
                        turn_id=turn_id,
                        source=source,
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

        reflection_signals: list[dict[str, Any]] = []
        for event in events:
            metadata = (
                event.get("metadata")
                if isinstance(event.get("metadata"), dict)
                else {}
            )
            if event.get("type") not in {"preference_change", "workflow_signal"}:
                continue
            reflection_signals.append(
                {
                    "entry_type": metadata.get("entry_type") or "preference",
                    "key": metadata.get("key"),
                    "value": metadata.get("value"),
                    "scope": metadata.get("scope") or "user",
                    "explicit_durable": bool(metadata.get("explicit_durable")),
                    "evidence_quote": metadata.get("evidence_quote")
                    or ((event.get("evidence") or [{}])[0].get("quote")),
                }
            )

        tool_names = [
            str(_tool_value(call, "name", "") or "") for call in tool_calls
        ]
        changed_paths: list[str] = []
        for call in tool_calls:
            changed_paths.extend(_collect_paths(redact_secrets(_tool_value(call, "arguments", {}))))
            changed_paths.extend(_collect_paths(redact_secrets(_tool_value(call, "output", {}))))
        return {
            "events": events,
            "reflection_signals": reflection_signals,
            "prospective_specs": [
                {"content": content, "trigger": trigger}
                for content, trigger in _prospective_specs(redact_text(user_text))
            ],
            "prospective_context": {
                "workspace": workspace_key,
                "text": "\n".join(
                    [redact_text(user_text), redact_text(assistant_text)]
                ),
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
            },
            "llm_used": llm_used,
            "llm_rejected": llm_rejected,
        }

    def _capture_user_model(
        self,
        *,
        capture_id: str,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry_ids: list[str] = []
        index = 0
        for event in events:
            metadata = (
                event.get("metadata")
                if isinstance(event.get("metadata"), dict)
                else {}
            )
            if event.get("type") not in {"preference_change", "workflow_signal"}:
                continue
            if not bool(metadata.get("explicit_durable")) or not metadata.get("key"):
                continue
            scope = str(metadata.get("scope") or "user")
            row = self.user_model.upsert_by_key(
                entry_type=str(metadata.get("entry_type") or "preference"),
                key=str(metadata.get("key")),
                value=metadata.get("value"),
                source="user_explicit",
                confidence=1.0,
                scope=scope,
                workspace_key=workspace_key if scope == "workspace" else "",
                session_id=session_id if scope == "session" else "",
                source_turn_id=turn_id,
                change_reason=str(
                    metadata.get("change_reason")
                    or event.get("reason")
                    or "explicit user change"
                ),
                evidence=list(event.get("evidence") or []),
                idempotency_key=f"{capture_id}:user_model:{index}",
            )
            entry_ids.append(str(row.get("entry_id") or ""))
            index += 1
        return {"user_model_entry_ids": entry_ids}

    def _capture_state(
        self,
        *,
        capture_id: str,
        session_id: str,
        turn_id: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.state is None:
            return {"state_version": None}
        state_ops: list[dict[str, Any]] = []
        goal = next(
            (
                event
                for event in events
                if event.get("type") == "goal"
                and str(((event.get("evidence") or [{}])[0]).get("source") or "")
                == "user"
            ),
            None,
        )
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
                        "authority": "user_explicit",
                        "evidence_quote": quote,
                    },
                ]
            )
        terminal = next((event for event in events if _is_terminal_event(event)), None)
        has_current_goal = "session:current_goal" in (
            self.state.get(session_id).get("entities") or {}
        )
        if terminal is not None and (goal is not None or has_current_goal):
            quote = str(((terminal.get("evidence") or [{}])[0]).get("quote") or "")
            metadata = terminal.get("metadata") or {}
            kind = str(metadata.get("outcome_kind") or "")
            state_ops.append(
                {
                    "op": "set_status",
                    "entity_id": "session:current_goal",
                    "status": (
                        "done"
                        if kind in {"completed", "verified_completion"}
                        else "cancelled"
                    ),
                    "authority": metadata.get("authority"),
                    "evidence_quote": quote,
                }
            )
        if not state_ops:
            return {"state_version": None}
        evidence_text = "\n".join(
            str(ref.get("quote") or "")
            for event in events
            for ref in event.get("evidence") or []
        )
        result = self.state.apply_ops(
            session_id=session_id,
            operations=state_ops,
            source_turn_id=turn_id,
            evidence_text=evidence_text,
            idempotency_key=f"{capture_id}:state",
        )
        return {"state_version": int(result.get("version") or 0)}

    def _capture_episode(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
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
            close_episode=any(_is_terminal_event(event) for event in events),
        )
        return {
            "episode_id": episode.get("episode_id"),
            "event_ids": [
                str(event.get("event_id") or "")
                for event in episode.get("events") or []
                if event.get("turn_id") == turn_id
            ],
        }

    def _state_store_identity(self) -> str:
        if self.state is None:
            return "conversation-state:none"
        return self.state.store_identity

    def _resume_record(
        self,
        record: dict[str, Any],
        *,
        workspace_key: str,
        state_store_identity: str,
    ) -> dict[str, Any]:
        """Finish one journal record from its durable prepared plan only."""

        capture_id = str(record.get("capture_id") or "")
        if str(record.get("workspace_key") or "") != workspace_key:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_AFFINITY",
                    "pending capture belongs to another workspace",
                    capture_id=capture_id,
                )
            )
        if record.get("state_store_identity") != state_store_identity:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_AFFINITY",
                    "pending capture belongs to another state store",
                    capture_id=capture_id,
                )
            )
        if record.get("status") == "completed":
            report = dict(record.get("report") or {})
            report["idempotent_replay"] = True
            return report
        session_id = str(record.get("session_id") or "")
        turn_id = str(record.get("turn_id") or "")
        workspace_key = str(record.get("workspace_key") or "")
        prepared = dict(record.get("prepared") or {})
        if not capture_id or not session_id or not turn_id or not isinstance(
            prepared.get("events"), list
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID",
                    "automatic-capture journal has no valid prepared event plan",
                    capture_id=capture_id,
                )
            )
        events = [dict(event) for event in prepared.get("events") or []]
        stage_rows = dict(record.get("stages") or {})

        def prior(stage: str) -> dict[str, Any] | None:
            row = stage_rows.get(stage)
            if isinstance(row, dict) and row.get("status") == "done":
                return dict(row.get("result") or {})
            return None

        user_model_result = prior("user_model")
        if user_model_result is None:
            user_model_result = self._capture_user_model(
                capture_id=capture_id,
                session_id=session_id,
                turn_id=turn_id,
                workspace_key=workspace_key,
                events=events,
            )
            self.journal.mark_stage(
                capture_id=capture_id,
                stage="user_model",
                stage_result=user_model_result,
            )

        state_result = prior("state")
        if state_result is None:
            state_result = self._capture_state(
                capture_id=capture_id,
                session_id=session_id,
                turn_id=turn_id,
                events=events,
            )
            self.journal.mark_stage(
                capture_id=capture_id,
                stage="state",
                stage_result=state_result,
            )

        episode_result = prior("episode")
        if episode_result is None:
            episode_result = self._capture_episode(
                session_id=session_id,
                turn_id=turn_id,
                workspace_key=workspace_key,
                events=events,
            )
            self.journal.mark_stage(
                capture_id=capture_id,
                stage="episode",
                stage_result=episode_result,
            )

        reflection_result = prior("reflection")
        if reflection_result is None:
            reflection_ids: list[str] = []
            signals = [
                dict(item) for item in prepared.get("reflection_signals") or []
            ]
            if self.reflection is not None and signals:
                candidates = self.reflection.observe(
                    session_id=session_id,
                    turn_id=turn_id,
                    signals=signals,
                    idempotency_key=f"{capture_id}:reflection",
                )
                reflection_ids = [
                    str(row.get("candidate_id") or "") for row in candidates
                ]
            reflection_result = {"reflection_candidate_ids": reflection_ids}
            self.journal.mark_stage(
                capture_id=capture_id,
                stage="reflection",
                stage_result=reflection_result,
            )

        prospective_result = prior("prospective")
        if prospective_result is None:
            prospective_ids: list[str] = []
            triggered_ids: list[str] = []
            if self.prospective is not None:
                triggered = self.prospective.match(
                    context=dict(prepared.get("prospective_context") or {}),
                    idempotency_key=f"{capture_id}:prospective:match",
                )
                triggered_ids = [
                    str(row.get("entry_id") or "") for row in triggered
                ]
                for index, spec in enumerate(prepared.get("prospective_specs") or []):
                    row = self.prospective.create(
                        content=str(spec.get("content") or ""),
                        trigger=dict(spec.get("trigger") or {}),
                        source_session_id=session_id,
                        source_turn_id=turn_id,
                        idempotency_key=f"{capture_id}:prospective:create:{index}",
                    )
                    prospective_ids.append(str(row.get("entry_id") or ""))
            prospective_result = {
                "prospective_entry_ids": prospective_ids,
                "triggered_prospective_ids": triggered_ids,
            }
            self.journal.mark_stage(
                capture_id=capture_id,
                stage="prospective",
                stage_result=prospective_result,
            )

        report = {
            "status": (
                "used"
                if events or prospective_result.get("prospective_entry_ids")
                else "skipped"
            ),
            **episode_result,
            **user_model_result,
            **state_result,
            **reflection_result,
            **prospective_result,
            "llm_used": bool(prepared.get("llm_used")),
            "llm_rejected": int(prepared.get("llm_rejected") or 0),
            "capture_id": capture_id,
        }
        return self.journal.complete(capture_id=capture_id, report=report)

    def resume_pending(
        self, *, workspace_key: str, limit: int | None = None
    ) -> dict[str, Any]:
        recovered: list[str] = []
        failures: list[dict[str, str]] = []
        batch_limit = self.resume_batch_size if limit is None else limit
        state_store_identity = self._state_store_identity()
        for record in self.journal.list_pending(
            workspace_key=workspace_key,
            limit=batch_limit,
        ):
            capture_id = str(record.get("capture_id") or "")
            try:
                self._resume_record(
                    record,
                    workspace_key=workspace_key,
                    state_store_identity=state_store_identity,
                )
                recovered.append(capture_id)
            except Exception as exc:  # noqa: BLE001 - persisted and reported
                error_code = (
                    exc.error.code
                    if isinstance(exc, AriadneError)
                    else "ARIADNE_MEMORY_CAPTURE_RESUME_FAILED"
                )
                error_message = redact_text(
                    exc.error.message if isinstance(exc, AriadneError) else str(exc)
                )[:500]
                failure = {
                    "capture_id": capture_id,
                    "error_code": error_code,
                    "error_message": error_message,
                }
                try:
                    self.journal.note_resume_failure(
                        capture_id=capture_id,
                        error_code=error_code,
                        error_message=error_message,
                    )
                except Exception as journal_exc:  # noqa: BLE001 - still observable
                    failure["journal_error"] = redact_text(str(journal_exc))[:500]
                failures.append(failure)
        return {
            "recovered_capture_ids": recovered,
            "recovery_failures": failures,
        }

    @staticmethod
    def _with_recovery(
        report: dict[str, Any], recovery: dict[str, Any]
    ) -> dict[str, Any]:
        result = dict(report)
        current_status = str(result.get("status") or "skipped")
        result["capture_status"] = current_status
        result["recovered_capture_ids"] = list(
            recovery.get("recovered_capture_ids") or []
        )
        result["recovery_failures"] = list(recovery.get("recovery_failures") or [])
        if result["recovery_failures"]:
            result["status"] = "failed"
        return result

    async def capture_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        workspace_key: str,
        user_text: str,
        assistant_text: str,
        tool_calls: list[Any] | None = None,
        verified_goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state_store_identity = self._state_store_identity()
        recovery = self.resume_pending(workspace_key=workspace_key)
        calls = list(tool_calls or [])
        digest_payload = {
            "user_text": redact_text(user_text),
            "assistant_text": redact_text(assistant_text),
            "tool_evidence": _sanitized_tool_evidence(calls),
            "verified_goal": redact_secrets(dict(verified_goal or {})),
        }
        input_digest = _payload_digest(digest_payload)
        existing = self.journal.get(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
        )
        prepared: dict[str, Any] = {}
        if existing is None:
            prepared = await self._prepare(
                session_id=session_id,
                turn_id=turn_id,
                workspace_key=workspace_key,
                user_text=user_text,
                assistant_text=assistant_text,
                tool_calls=calls,
                verified_goal=verified_goal,
            )
        record = self.journal.start(
            workspace_key=workspace_key,
            session_id=session_id,
            turn_id=turn_id,
            input_digest=input_digest,
            state_store_identity=state_store_identity,
            prepared=prepared,
        )
        report = self._resume_record(
            record,
            workspace_key=workspace_key,
            state_store_identity=state_store_identity,
        )
        return self._with_recovery(report, recovery)


def make_llm_memory_extractor(model: Any) -> MemoryExtractorFn:
    """Create an ambiguity-only, evidence-quoting structured extractor."""

    async def extract(payload: dict[str, Any]) -> list[dict[str, Any]]:
        system = (
            "Extract memory events only from the supplied completed turn. Reply JSON only as "
            '{"events":[{"type":"...","content":"...","evidence_quote":"verbatim",'
            '"entities":[],"reason":"","relation":null,"metadata":{}}]}. '
            "Allowed types: problem, goal, hypothesis, attempt, observation, error, decision, outcome, "
            "preference_change, workflow_signal, entity_change. evidence_quote must be verbatim. "
            "Do not invent facts, ids, prior conversation, or durable preferences. "
            'Return {"events":[]} when uncertain.'
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
        try:
            obj = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                    "memory extractor returned invalid JSON",
                    line=exc.lineno,
                    column=exc.colno,
                )
            ) from exc
        if not isinstance(obj, dict) or set(obj) != {"events"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                    "memory extractor response must be exactly an events object",
                )
            )
        events = obj.get("events")
        if not isinstance(events, list) or len(events) > 32:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                    "memory extractor events must be an array with at most 32 items",
                )
            )
        allowed_fields = {
            "type",
            "content",
            "evidence_quote",
            "entities",
            "reason",
            "relation",
            "metadata",
        }
        normalized: list[dict[str, Any]] = []
        for index, row in enumerate(events):
            if not isinstance(row, dict) or not {"type", "content", "evidence_quote"} <= set(row):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                        "memory extractor event has an invalid shape",
                        index=index,
                    )
                )
            if set(row) - allowed_fields:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                        "memory extractor event contains unknown fields",
                        index=index,
                        unknown=sorted(set(row) - allowed_fields),
                    )
                )
            event_type = row.get("type")
            content = row.get("content")
            quote = row.get("evidence_quote")
            entities = row.get("entities", [])
            reason = row.get("reason", "")
            relation = row.get("relation")
            metadata = row.get("metadata", {})
            if (
                not isinstance(event_type, str)
                or event_type not in EPISODE_EVENT_TYPES
                or not isinstance(content, str)
                or not 1 <= len(content.strip()) <= 2000
                or not isinstance(quote, str)
                or not 1 <= len(quote.strip()) <= 800
                or not isinstance(entities, list)
                or len(entities) > 24
                or any(not isinstance(item, str) or len(item) > 200 for item in entities)
                or not isinstance(reason, str)
                or len(reason) > 1000
                or (relation is not None and not isinstance(relation, dict))
                or not isinstance(metadata, dict)
            ):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                        "memory extractor event fields violate the protocol",
                        index=index,
                    )
                )
            normalized.append(dict(row))
        return normalized

    return extract
