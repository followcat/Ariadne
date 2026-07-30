from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..errors import AriadneError, app_error
from ..tools.registry import ToolSpec
from ..types import ToolCallTrace
from .fingerprint import workspace_fingerprint
from .models import EvidenceRef, Observation, OpenQuestion, Step, TaskState
from .store import SQLiteTaskStore
from .verify import DeterministicVerifier


SUBMIT_TASK_PLAN_NAME = "submit_task_plan"
SUBMIT_TASK_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SUBMIT_TASK_PLAN_NAME,
        "description": (
            "Submit a verifiable plan for task mode. This is a kernel control call, "
            "not an external capability. Every step requires deterministic done_when checks."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal", "steps"],
            "properties": {
                "goal": {"type": "string", "minLength": 1},
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["intent", "done_when"],
                        "properties": {
                            "intent": {"type": "string", "minLength": 1},
                            "preconditions": {"type": "array", "items": {"$ref": "#/$defs/check"}},
                            "done_when": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"$ref": "#/$defs/check"},
                            },
                            "tools_hint": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "max_retries": {"type": "integer", "minimum": 0, "maximum": 3},
                            "failure_policy": {
                                "type": "string",
                                "enum": ["retry", "replan", "ask_user", "abort"],
                            },
                        },
                    },
                },
            },
            "$defs": {
                "check": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "spec"],
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["command_exit", "path_exists", "path_absent", "file_contains"],
                        },
                        "spec": {"type": "object"},
                        "required": {"type": "boolean"},
                    },
                }
            },
        },
    },
}


@dataclass(slots=True)
class TaskAttemptOutcome:
    state: TaskState
    step: Step
    safe_retry: bool


@dataclass(slots=True)
class TaskController:
    store: SQLiteTaskStore
    verifier: DeterministicVerifier

    @property
    def workspace(self):
        return self.verifier.workspace

    def load_active(self, session_id: str) -> TaskState | None:
        return self.store.load_active(session_id)

    def create_from_plan(
        self,
        *,
        session_id: str,
        user_id: str | None,
        arguments: dict[str, Any],
    ) -> TaskState:
        if self.store.load_active(session_id) is not None:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_ACTIVE_EXISTS",
                    "continue or cancel the active task before starting another",
                    session_id=session_id,
                )
            )
        try:
            Draft202012Validator(
                SUBMIT_TASK_PLAN_TOOL["function"]["parameters"]
            ).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    f"task plan failed JSON Schema validation: {exc.message}",
                    path=path,
                )
            ) from exc
        raw_steps = arguments.get("steps")
        if not isinstance(raw_steps, list):
            raise AriadneError(app_error("ARIADNE_TASK_INVALID", "steps must be an array"))
        state = TaskState.from_plan(
            session_id=session_id,
            user_id=user_id,
            goal=str(arguments.get("goal") or ""),
            steps=raw_steps,
            goal_checks=[],
            workspace_fingerprint=workspace_fingerprint(self.workspace),
        )
        return self.store.save(state, expected_revision=0)

    def prepare_resume(self, state: TaskState) -> TaskState:
        previous_revision = state.revision
        changed = False
        current_fingerprint = workspace_fingerprint(self.workspace)
        if current_fingerprint != state.workspace_fingerprint:
            state.workspace_fingerprint = current_fingerprint
            for assumption in state.assumptions:
                if assumption.status == "current":
                    assumption.status = "stale"
                    changed = True
            changed = True
        step = state.current_step
        if step is not None and step.status == "running":
            attempt_id = f"{step.step_id}:resume"
            results = self.verifier.run_many(
                step.done_when,
                traces=[],
                attempt_id=attempt_id,
                resume=True,
            )
            step.check_results = results
            required = {
                result.check_id: result
                for result in results
                if next(c for c in step.done_when if c.check_id == result.check_id).required
            }
            if required and all(result.status == "pass" for result in required.values()):
                step.status = "verified"
                self._advance(state)
            changed = True
        if changed:
            self.store.save(state, expected_revision=previous_revision)
        return state

    def continue_with_user_input(self, state: TaskState, text: str) -> TaskState:
        if state.status != "needs_input":
            return state
        previous_revision = state.revision
        attempt_id = f"user:{uuid.uuid4().hex[:8]}"
        evidence = EvidenceRef(
            evidence_id=f"evidence_{uuid.uuid4().hex[:12]}",
            kind="user",
            ref=state.session_id,
            summary=str(text)[:500],
            attempt_id=attempt_id,
        )
        state.last_observation = Observation(
            observation_id=f"observation_{uuid.uuid4().hex[:12]}",
            kind="user",
            summary=str(text)[:500],
            evidence=[evidence],
        )
        state.open_questions.clear()
        state.status = "active"
        return self.store.save(state, expected_revision=previous_revision)

    def start_attempt(self, state: TaskState) -> tuple[TaskState, Step, str]:
        step = state.current_step
        if state.status != "active" or step is None:
            raise AriadneError(
                app_error("ARIADNE_TASK_PROTOCOL_ERROR", "task has no runnable current step")
            )
        previous_revision = state.revision
        if step.status == "pending":
            step.status = "running"
        if step.status != "running":
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_PROTOCOL_ERROR",
                    f"current step is not runnable: {step.status}",
                )
            )
        precondition_attempt = f"{step.step_id}:precondition:{step.attempt + 1}"
        step.precondition_results = self.verifier.run_many(
            step.preconditions,
            traces=[],
            attempt_id=precondition_attempt,
        )
        required_preconditions = [
            result
            for result in step.precondition_results
            if next(
                check for check in step.preconditions if check.check_id == result.check_id
            ).required
        ]
        if required_preconditions and not all(
            result.status == "pass" for result in required_preconditions
        ):
            state.status = "needs_input"
            state.open_questions = [
                OpenQuestion(
                    question_id=f"question_{uuid.uuid4().hex[:12]}",
                    prompt="The current step preconditions are not satisfied.",
                )
            ]
            self.store.save(state, expected_revision=previous_revision)
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_PRECONDITION_FAILED",
                    "current step preconditions are not satisfied",
                    task_id=state.task_id,
                    step_id=step.step_id,
                    results=[
                        {"check_id": result.check_id, "status": result.status}
                        for result in required_preconditions
                    ],
                )
            )
        step.attempt += 1
        state = self.store.save(state, expected_revision=previous_revision)
        return state, step, f"{step.step_id}:{step.attempt}"

    def record_attempt(
        self,
        state: TaskState,
        *,
        traces: Iterable[ToolCallTrace],
        spec: ToolSpec | None,
        attempt_id: str,
    ) -> TaskAttemptOutcome:
        step = state.current_step
        if step is None:
            raise AriadneError(
                app_error("ARIADNE_TASK_PROTOCOL_ERROR", "task has no current step")
            )
        previous_revision = state.revision
        trace_list = list(traces)
        for trace in trace_list:
            evidence = EvidenceRef(
                evidence_id=f"evidence_{uuid.uuid4().hex[:12]}",
                kind="tool_result",
                ref=trace.call_id,
                summary=f"{trace.name} status={trace.status}",
                attempt_id=attempt_id,
            )
            step.evidence.append(evidence)
        step.check_results = self.verifier.run_many(
            step.done_when,
            traces=trace_list,
            attempt_id=attempt_id,
        )
        required_results = [
            result
            for result in step.check_results
            if next(check for check in step.done_when if check.check_id == result.check_id).required
        ]
        all_required = bool(required_results) and all(
            result.status == "pass" for result in required_results
        )
        summary = ", ".join(
            f"{result.check_id}={result.status}" for result in step.check_results
        )
        state.last_observation = Observation(
            observation_id=f"observation_{uuid.uuid4().hex[:12]}",
            kind="check_result",
            summary=summary,
            evidence=[ev for result in step.check_results for ev in result.evidence],
        )
        safe_retry = bool(
            spec is not None
            and (
                spec.side_effect_level in {"none", "read"}
                or spec.idempotent is True
            )
        )
        if all_required:
            step.status = "verified"
            self._advance(state)
        else:
            has_error = any(result.status == "error" for result in required_results)
            retry_available = step.attempt <= step.max_retries and safe_retry
            if step.failure_policy == "retry" and retry_available and not has_error:
                step.status = "running"
            elif step.failure_policy == "abort":
                step.status = "failed"
                state.status = "failed"
            else:
                state.status = "needs_input"
                reason = (
                    "Verification infrastructure failed; review the check evidence."
                    if has_error
                    else "The current step was not verified. Provide guidance or revise the plan."
                )
                if step.failure_policy == "retry" and not safe_retry:
                    reason = "Automatic retry is unsafe for this tool; confirm the next action."
                state.open_questions = [
                    OpenQuestion(
                        question_id=f"question_{uuid.uuid4().hex[:12]}",
                        prompt=reason,
                    )
                ]
        state = self.store.save(state, expected_revision=previous_revision)
        return TaskAttemptOutcome(state=state, step=step, safe_retry=safe_retry)

    def ask_user(self, state: TaskState, text: str) -> TaskState:
        previous_revision = state.revision
        state.status = "needs_input"
        state.open_questions = [
            OpenQuestion(
                question_id=f"question_{uuid.uuid4().hex[:12]}",
                prompt=text or "The task needs more input.",
            )
        ]
        return self.store.save(state, expected_revision=previous_revision)

    def fail(self, state: TaskState, *, message: str) -> TaskState:
        previous_revision = state.revision
        step = state.current_step
        if step is not None and step.status == "running":
            step.status = "failed"
        state.status = "failed"
        state.last_observation = Observation(
            observation_id=f"observation_{uuid.uuid4().hex[:12]}",
            kind="environment",
            summary=message,
        )
        return self.store.save(state, expected_revision=previous_revision)

    @staticmethod
    def _advance(state: TaskState) -> None:
        next_step = next((step for step in state.steps if step.status == "pending"), None)
        if next_step is None:
            state.current_step_id = None
            state.status = "completed"
        else:
            state.current_step_id = next_step.step_id
            state.status = "active"

    @staticmethod
    def format_context(state: TaskState) -> str:
        step = state.current_step
        payload = {
            "task_id": state.task_id,
            "status": state.status,
            "goal": state.goal,
            "current_step": (
                {
                    "step_id": step.step_id,
                    "intent": step.intent,
                    "status": step.status,
                    "attempt": step.attempt,
                    "done_when": [
                        {"check_id": c.check_id, "kind": c.kind, "spec": c.spec}
                        for c in step.done_when
                    ],
                }
                if step is not None
                else None
            ),
            "progress": [
                {"step_id": item.step_id, "status": item.status, "intent": item.intent}
                for item in state.steps
            ],
            "open_questions": [q.prompt for q in state.open_questions],
        }
        return (
            "[TASK_STATE authoritative]\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\nUse at most one material capability call in this exchange. "
            "Do not claim completion until the kernel reports verified/completed."
        )

    @staticmethod
    def plan_prompt(user_goal: str) -> str:
        return (
            "[TASK_MODE plan_required]\n"
            "Submit a concise executable plan with submit_task_plan before using capabilities. "
            "Every step needs deterministic done_when checks. Supported checks: "
            "command_exit (references sandbox_exec in the same attempt), path_exists, "
            "path_absent, file_contains. Use /workspace-relative paths. "
            f"The user's requested objective is: {user_goal}"
        )
