"""Kernel helpers for task control exchanges (plan / replan).

Keeps TurnApplication thinner: parse and validate control tool calls only.
Persistence and domain rules stay in TaskController.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import AriadneError, app_error
from .controller import REVISE_TASK_PLAN_NAME, SUBMIT_TASK_PLAN_NAME


def tool_call_name(call: dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


def payload_has_tool(payload: list[dict[str, Any]], name: str) -> bool:
    return any(tool_call_name(call) == name for call in payload)


def require_sole_control_call(
    payload: list[dict[str, Any]],
    *,
    name: str,
    allow_when_task_exists: bool,
    task_exists: bool,
) -> dict[str, Any]:
    """Return the single control tool call or raise ARIADNE_TASK_PROTOCOL_ERROR."""
    if name == SUBMIT_TASK_PLAN_NAME and task_exists:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_PROTOCOL_ERROR",
                "submit_task_plan is valid only before a task exists",
            )
        )
    if name == REVISE_TASK_PLAN_NAME and not task_exists and not allow_when_task_exists:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_PROTOCOL_ERROR",
                "revise_task_plan requires an active task",
            )
        )
    if len(payload) != 1 or tool_call_name(payload[0]) != name:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_PROTOCOL_ERROR",
                f"{name} must be the only call in an exchange",
            )
        )
    return payload[0]


def parse_control_arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_INVALID",
                f"task plan arguments are not valid JSON: {exc}",
            )
        ) from exc
    if not isinstance(args, dict):
        raise AriadneError(
            app_error("ARIADNE_TASK_INVALID", "task plan must be an object")
        )
    return args


def control_call_id(call: dict[str, Any]) -> str:
    import uuid

    return str(call.get("id") or uuid.uuid4().hex)
