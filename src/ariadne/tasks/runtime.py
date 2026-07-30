"""Task-mode attempt orchestration extracted from TurnApplication.

Keeps capability-call classification, attempt start, and post-attempt
verification event packaging out of the giant turn loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import AriadneError, app_error
from ..tools.registry import ToolRegistry
from ..types import ToolCallTrace, TurnEvent
from .controller import TaskAttemptOutcome, TaskController
from .models import TaskState

# Capabilities that may accompany a material call without counting as the
# single material action (discovery / skill load).
TASK_META_CAPABILITIES = frozenset(
    {
        "search_skills",
        "load_skill",
        "adopt_skill",
        "tool_search",
    }
)


@dataclass(slots=True)
class CapabilityExchangePlan:
    """Result of validating tool_calls_payload before invoke."""

    state: TaskState
    attempt_id: str = ""
    attempt_spec: Any = None
    attempt_effect: str = "unknown"
    step_started_event: TurnEvent | None = None


@dataclass(slots=True)
class AttemptFinalizeResult:
    state: TaskState
    outcome_row: dict[str, Any]
    events: list[TurnEvent] = field(default_factory=list)
    context_system_text: str = ""


def _parse_effect_args(raw: Any) -> dict[str, Any]:
    try:
        args = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return args if isinstance(args, dict) else {}


def prepare_capability_exchange(
    *,
    controller: TaskController,
    tools: ToolRegistry,
    state: TaskState,
    tool_calls_payload: list[dict[str, Any]],
) -> CapabilityExchangePlan:
    """Classify calls, enforce one material capability, maybe start_attempt."""
    capability_names = [
        str((call.get("function") or {}).get("name") or "")
        for call in tool_calls_payload
    ]
    capability_specs: list[tuple[Any, str]] = []
    material_specs: list[tuple[Any, str]] = []
    for capability_name, capability_call in zip(
        capability_names, tool_calls_payload, strict=True
    ):
        spec = tools.get(capability_name)
        raw_effect_args = (capability_call.get("function") or {}).get("arguments") or "{}"
        effect_args = _parse_effect_args(raw_effect_args)
        effect = (
            spec.effect_for(effect_args)
            if spec is not None and isinstance(effect_args, dict)
            else "unknown"
        )
        if capability_name not in TASK_META_CAPABILITIES:
            capability_specs.append((spec, effect))
        if effect not in {"none", "read"}:
            material_specs.append((spec, effect))
    if len(material_specs) > 1:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_PROTOCOL_ERROR",
                "task mode permits at most one material capability call per exchange",
                names=capability_names,
            )
        )
    if not capability_specs:
        return CapabilityExchangePlan(state=state)

    attempt_spec, attempt_effect = (
        material_specs[0] if material_specs else capability_specs[0]
    )
    state, task_step, attempt_id = controller.start_attempt(state)
    return CapabilityExchangePlan(
        state=state,
        attempt_id=attempt_id,
        attempt_spec=attempt_spec,
        attempt_effect=attempt_effect,
        step_started_event=TurnEvent(
            "task_step_started",
            {
                "task_id": state.task_id,
                "step_id": task_step.step_id,
                "attempt": task_step.attempt,
                "intent": task_step.intent,
            },
        ),
    )


async def finalize_attempt(
    *,
    controller: TaskController,
    state: TaskState,
    traces: list[ToolCallTrace],
    attempt_spec: Any,
    attempt_effect: str,
    attempt_id: str,
    skill_names: list[str] | None = None,
) -> AttemptFinalizeResult:
    """Run verification after tools and package events + outcome ledger row."""
    outcome: TaskAttemptOutcome = await controller.record_attempt_async(
        state,
        traces=traces,
        spec=attempt_spec,
        effect_level=attempt_effect,
        attempt_id=attempt_id,
    )
    state = outcome.state
    events: list[TurnEvent] = []
    for check_result in outcome.step.check_results:
        events.append(
            TurnEvent(
                "task_check_completed",
                {
                    "task_id": state.task_id,
                    "step_id": outcome.step.step_id,
                    "check_id": check_result.check_id,
                    "status": check_result.status,
                    "observed_value": check_result.observed_value,
                    "error": (
                        {
                            "code": check_result.error.code,
                            "message": check_result.error.message,
                        }
                        if check_result.error
                        else None
                    ),
                },
            )
        )
    if state.status == "completed":
        events.append(
            TurnEvent(
                "task_completed",
                {"task_id": state.task_id, "revision": state.revision},
            )
        )
    elif state.status == "failed":
        events.append(
            TurnEvent(
                "task_failed",
                {"task_id": state.task_id, "revision": state.revision},
            )
        )
    elif state.status == "needs_input" and state.open_questions:
        events.append(
            TurnEvent(
                "task_needs_input",
                {
                    "task_id": state.task_id,
                    "current_step_id": state.current_step_id,
                    "question": state.open_questions[0].prompt,
                },
            )
        )
    row = {
        "task_id": state.task_id,
        "step_id": outcome.step.step_id,
        "attempt_id": attempt_id,
        "step_outcome": outcome.step.status,
        "task_outcome": state.status,
        "skills": list(skill_names or []),
        "tool_names": [trace.name for trace in traces if trace.name],
    }
    return AttemptFinalizeResult(
        state=state,
        outcome_row=row,
        events=events,
        context_system_text=controller.format_context(state),
    )


def resolve_final_answer_status(
    *,
    controller: TaskController,
    state: TaskState | None,
    assistant_text: str,
) -> tuple[str, Any, TaskState | None, TurnEvent | None]:
    """Map task state to turn status when the model finishes without tools.

    Returns ``(turn_status, turn_error, state, optional_event)``.
    """
    if state is None:
        return (
            "failed",
            app_error(
                "ARIADNE_TASK_PLAN_REQUIRED",
                "task mode requires submit_task_plan before an answer",
            ),
            None,
            None,
        )
    if state.status == "completed":
        return "completed", None, state, None
    if state.status in {"failed", "cancelled"}:
        return (
            "failed",
            app_error(
                "ARIADNE_TASK_FAILED",
                state.last_observation.summary
                if state.last_observation is not None
                else f"task ended with status {state.status}",
                task_id=state.task_id,
            ),
            state,
            None,
        )
    state = controller.ask_user(
        state,
        assistant_text or "The task is not verified and needs more input.",
    )
    event = TurnEvent(
        "task_needs_input",
        {
            "task_id": state.task_id,
            "current_step_id": state.current_step_id,
            "question": state.open_questions[0].prompt,
        },
    )
    return "needs_input", None, state, event
