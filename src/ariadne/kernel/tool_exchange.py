"""Single-exchange tool invocation extracted from TurnApplication.

Owns parse → invoke → trace → tool/skill events for one model tool_calls
payload. Task attempt start/finalize stays in tasks.runtime; turn only wires
context appends and thrash bookkeeping.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..errors import AriadneError, app_error
from ..redact import redact_secrets
from ..tools.registry import ToolContext, ToolRegistry, dumps_tool_output
from ..types import SkillEvent, ToolCallTrace, TurnEvent

_SKILL_TOOL_NAMES = frozenset({"search_skills", "load_skill", "adopt_skill"})


@dataclass(slots=True)
class RequiredMessage:
    """One required conversation message produced by a tool exchange."""

    message: dict[str, Any]
    source: str
    reason: str
    trust: str


@dataclass(slots=True)
class ToolExchangeResult:
    """Outcome of invoking all tool_calls in one model exchange."""

    traces: list[ToolCallTrace] = field(default_factory=list)
    events: list[TurnEvent] = field(default_factory=list)
    appends: list[RequiredMessage] = field(default_factory=list)
    evidence_snippets: list[str] = field(default_factory=list)
    observed_evidence_snippets: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)


def _parse_args(raw_args: Any) -> dict[str, Any]:
    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
    if not isinstance(args, dict):
        raise AriadneError(
            app_error(
                "ARIADNE_INVALID_TOOL_ARGS",
                "tool arguments must be a JSON object",
            )
        )
    return args


def _task_ids(
    *,
    task_id: str,
    step_id: str,
    attempt_id: str,
) -> tuple[str, str, str]:
    return task_id, step_id, attempt_id


def _failed_trace(
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    err: Any,
    started: datetime,
    finished: datetime,
    task_id: str,
    step_id: str,
    attempt_id: str,
) -> ToolCallTrace:
    return ToolCallTrace(
        call_id=call_id,
        name=name,
        arguments=arguments,
        output=None,
        status="failed",
        error=err,
        started_at=started,
        finished_at=finished,
        task_id=task_id,
        step_id=step_id,
        attempt_id=attempt_id,
    )


def _skill_side_effects(
    *,
    name: str,
    args: dict[str, Any],
    skill_events: list[SkillEvent],
    task_id: str,
    step_id: str,
    attempt_id: str,
    bind_skill_event: Callable[..., None] | None,
    pending_skill_adoptions: dict[str, SkillEvent] | None,
) -> TurnEvent | None:
    """Handle adopt_skill attempt attribution; return skill_event for stream."""
    if name not in _SKILL_TOOL_NAMES or not skill_events:
        return None
    if name == "adopt_skill":
        adopted_name = str(args.get("name") or "")
        adopted_event = next(
            (
                event
                for event in reversed(skill_events)
                if event.kind == "adopt" and event.skill_name == adopted_name
            ),
            None,
        )
        if adopted_event is not None and not adopted_event.attempt_id:
            if attempt_id and bind_skill_event is not None:
                bind_skill_event(
                    adopted_event,
                    task_id=task_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                )
            elif pending_skill_adoptions is not None:
                pending_skill_adoptions[adopted_name] = adopted_event
    last = skill_events[-1]
    return TurnEvent(
        "skill_event",
        {
            "kind": last.kind,
            "skill_name": last.skill_name,
            "detail": last.detail,
        },
    )


async def invoke_tool_exchange(
    *,
    tools: ToolRegistry,
    tool_calls_payload: list[dict[str, Any]],
    ctx: ToolContext,
    redact_traces: bool = True,
    task_id: str = "",
    step_id: str = "",
    attempt_id: str = "",
    skill_events: list[SkillEvent] | None = None,
    pending_skill_adoptions: dict[str, SkillEvent] | None = None,
    bind_skill_event: Callable[..., None] | None = None,
) -> ToolExchangeResult:
    """Invoke each capability call; package traces, stream events, and context."""
    result = ToolExchangeResult()
    skill_events = skill_events if skill_events is not None else []
    tid, sid, aid = _task_ids(
        task_id=task_id, step_id=step_id, attempt_id=attempt_id
    )

    for call in tool_calls_payload:
        call_id = str(call.get("id") or uuid.uuid4().hex)
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        if name:
            result.tool_names.append(name)
        raw_args = fn.get("arguments") or "{}"
        started = datetime.now(timezone.utc)
        result.events.append(
            TurnEvent("tool_started", {"call_id": call_id, "name": name})
        )
        args: dict[str, Any] = {}
        try:
            args = _parse_args(raw_args)
            output = await tools.invoke(name, args, ctx)
            trace_args = redact_secrets(args) if redact_traces else args
            if redact_traces:
                output = redact_secrets(output)
            finished = datetime.now(timezone.utc)
            spec = tools.get(name)
            trace = ToolCallTrace(
                call_id=call_id,
                name=name,
                arguments=trace_args if isinstance(trace_args, dict) else args,
                output=output,
                status="completed",
                started_at=started,
                finished_at=finished,
                schema_chars=spec.schema_chars() if spec else 0,
                task_id=tid,
                step_id=sid,
                attempt_id=aid,
            )
            result.traces.append(trace)
            out_text = dumps_tool_output(output)
            result.appends.append(
                RequiredMessage(
                    message={
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": out_text,
                    },
                    source=f"tool_result:{call_id}",
                    reason="verbatim capability result used as evidence",
                    trust="tool",
                )
            )
            snippet = out_text[:2000]
            result.evidence_snippets.append(snippet)
            result.observed_evidence_snippets.append(snippet)
            result.events.append(
                TurnEvent(
                    "tool_completed",
                    {
                        "call_id": call_id,
                        "name": name,
                        "status": "completed",
                        "arguments": (
                            trace_args if isinstance(trace_args, dict) else args
                        ),
                        "output": output,
                    },
                )
            )
            skill_ev = _skill_side_effects(
                name=name,
                args=args,
                skill_events=skill_events,
                task_id=tid,
                step_id=sid,
                attempt_id=aid,
                bind_skill_event=bind_skill_event,
                pending_skill_adoptions=pending_skill_adoptions,
            )
            if skill_ev is not None:
                result.events.append(skill_ev)
        except AriadneError as exc:
            finished = datetime.now(timezone.utc)
            err = exc.error
            fail_args = redact_secrets(args) if redact_traces else args
            if not isinstance(fail_args, dict):
                fail_args = args
            failed = _failed_trace(
                call_id=call_id,
                name=name,
                arguments=fail_args if isinstance(fail_args, dict) else args,
                err=err,
                started=started,
                finished=finished,
                task_id=tid,
                step_id=sid,
                attempt_id=aid,
            )
            result.traces.append(failed)
            result.appends.append(
                RequiredMessage(
                    message={
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": dumps_tool_output(
                            {
                                "error": {
                                    "code": err.code,
                                    "message": err.message,
                                    "details": err.details,
                                }
                            }
                        ),
                    },
                    source=f"tool_result:{call_id}",
                    reason="verbatim structured capability failure",
                    trust="tool",
                )
            )
            result.events.append(
                TurnEvent(
                    "tool_completed",
                    {
                        "call_id": call_id,
                        "name": name,
                        "status": "failed",
                        "arguments": fail_args if isinstance(fail_args, dict) else args,
                        "error": {"code": err.code, "message": err.message},
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            finished = datetime.now(timezone.utc)
            err = app_error(
                "ARIADNE_TOOL_HANDLER_ERROR",
                f"{type(exc).__name__}: {exc}",
            )
            failed = _failed_trace(
                call_id=call_id,
                name=name,
                arguments=args,
                err=err,
                started=started,
                finished=finished,
                task_id=tid,
                step_id=sid,
                attempt_id=aid,
            )
            result.traces.append(failed)
            result.appends.append(
                RequiredMessage(
                    message={
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": dumps_tool_output(
                            {"error": {"code": err.code, "message": err.message}}
                        ),
                    },
                    source=f"tool_result:{call_id}",
                    reason="verbatim handler failure evidence",
                    trust="tool",
                )
            )
            result.events.append(
                TurnEvent(
                    "tool_completed",
                    {
                        "call_id": call_id,
                        "name": name,
                        "status": "failed",
                        "arguments": args,
                        "error": {"code": err.code, "message": err.message},
                    },
                )
            )

    return result
