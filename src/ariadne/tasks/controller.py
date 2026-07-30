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
from .models import EvidenceRef, Observation, OpenQuestion, PlanRevision, Step, TaskState
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
                "goal_checks": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/check"},
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

REVISE_TASK_PLAN_NAME = "revise_task_plan"
REVISE_TASK_PLAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": REVISE_TASK_PLAN_NAME,
        "description": (
            "Replace the unverified remainder of a task after verification failure. "
            "The revision must cite evidence IDs supplied by TASK_STATE."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reason", "evidence_ids", "steps"],
            "properties": {
                "reason": {"type": "string", "minLength": 1},
                "evidence_ids": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
                "steps": SUBMIT_TASK_PLAN_TOOL["function"]["parameters"]["properties"]["steps"],
                "goal_checks": SUBMIT_TASK_PLAN_TOOL["function"]["parameters"]["properties"][
                    "goal_checks"
                ],
            },
            "$defs": SUBMIT_TASK_PLAN_TOOL["function"]["parameters"]["$defs"],
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
            goal_checks=list(arguments.get("goal_checks") or []),
            workspace_fingerprint=workspace_fingerprint(self.workspace),
        )
        return self.store.save(state, expected_revision=0)

    def revise_from_plan(self, state: TaskState, *, arguments: dict[str, Any]) -> TaskState:
        if not state.replan_required:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_PROTOCOL_ERROR",
                    "revise_task_plan is only valid when evidence requires a replan",
                )
            )
        try:
            Draft202012Validator(
                REVISE_TASK_PLAN_TOOL["function"]["parameters"]
            ).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    f"task revision failed JSON Schema validation: {exc.message}",
                    path=path,
                )
            ) from exc
        requested_ids = [str(value) for value in arguments.get("evidence_ids", [])]
        available = {evidence.evidence_id: evidence for evidence in state.replan_evidence}
        missing = sorted(set(requested_ids) - available.keys())
        if missing:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_REPLAN_EVIDENCE_INVALID",
                    "task revision cited evidence not present in the failed observation",
                    missing=missing,
                    available=sorted(available),
                )
            )
        previous_revision = state.revision
        prior_ids = [
            step.step_id for step in state.steps if step.status in {"pending", "running", "failed"}
        ]
        for step in state.steps:
            if step.status in {"pending", "running"}:
                step.status = "skipped"
        new_steps = [Step.from_plan(value) for value in arguments["steps"]]
        state.steps.extend(new_steps)
        state.current_step_id = new_steps[0].step_id
        if "goal_checks" in arguments:
            state.goal_checks = [
                # The model cannot supply IDs; kernel allocates them.
                check
                for raw in arguments.get("goal_checks", [])
                for check in [self._check_from_plan(raw)]
            ]
            state.goal_check_results.clear()
        evidence = [available[evidence_id] for evidence_id in requested_ids]
        state.plan_revisions.append(
            PlanRevision(
                revision=len(state.plan_revisions) + 1,
                reason=str(arguments["reason"]).strip(),
                evidence=evidence,
                prior_step_ids=prior_ids,
                new_step_ids=[step.step_id for step in new_steps],
            )
        )
        state.replan_required = False
        state.replan_reason = ""
        state.replan_evidence.clear()
        state.open_questions.clear()
        state.status = "active"
        return self.store.save(state, expected_revision=previous_revision)

    @staticmethod
    def _check_from_plan(value: dict[str, Any]):
        from .models import Check

        return Check.from_plan(value)

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
                has_more_steps = self._advance(state)
                if not has_more_steps and not state.goal_checks:
                    state.status = "completed"
            changed = True
        if state.status == "active" and state.current_step is None and state.goal_checks:
            state.goal_check_results = self.verifier.run_many(
                state.goal_checks,
                traces=[],
                attempt_id=f"{state.task_id}:goal:resume",
                resume=True,
            )
            required_goal_results = [
                result
                for result in state.goal_check_results
                if next(
                    check for check in state.goal_checks if check.check_id == result.check_id
                ).required
            ]
            if not required_goal_results or all(
                result.status == "pass" for result in required_goal_results
            ):
                state.status = "completed"
            else:
                state.status = "needs_input"
                state.open_questions = [
                    OpenQuestion(
                        question_id=f"question_{uuid.uuid4().hex[:12]}",
                        prompt="Goal checks require fresh evidence after resume.",
                    )
                ]
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
        effect_level: str = "unknown",
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
                effect_level in {"none", "read"}
                or spec.idempotent is True
            )
        )
        if all_required:
            step.status = "verified"
            has_more_steps = self._advance(state)
            if not has_more_steps:
                state.goal_check_results = self.verifier.run_many(
                    state.goal_checks,
                    traces=trace_list,
                    attempt_id=f"{attempt_id}:goal",
                )
                required_goal_results = [
                    result
                    for result in state.goal_check_results
                    if next(
                        check for check in state.goal_checks if check.check_id == result.check_id
                    ).required
                ]
                goal_verified = not required_goal_results or all(
                    result.status == "pass" for result in required_goal_results
                )
                if goal_verified:
                    state.status = "completed"
                else:
                    state.status = "needs_input"
                    state.open_questions = [
                        OpenQuestion(
                            question_id=f"question_{uuid.uuid4().hex[:12]}",
                            prompt="All steps ran, but the goal checks were not verified.",
                        )
                    ]
        else:
            has_error = any(result.status == "error" for result in required_results)
            retry_available = step.attempt <= step.max_retries and safe_retry
            if step.failure_policy == "retry" and retry_available and not has_error:
                step.status = "running"
            elif step.failure_policy == "replan" and not has_error:
                step.status = "failed"
                state.status = "active"
                state.replan_required = True
                state.replan_reason = "Required done_when checks did not pass."
                state.replan_evidence = [
                    evidence
                    for result in required_results
                    for evidence in result.evidence
                ] or list(step.evidence[-len(trace_list) :])
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
    def _advance(state: TaskState) -> bool:
        next_step = next((step for step in state.steps if step.status == "pending"), None)
        if next_step is None:
            state.current_step_id = None
            return False
        else:
            state.current_step_id = next_step.step_id
            state.status = "active"
            return True

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
            "replan": (
                {
                    "required": True,
                    "reason": state.replan_reason,
                    "evidence_ids": [
                        evidence.evidence_id for evidence in state.replan_evidence
                    ],
                }
                if state.replan_required
                else {"required": False}
            ),
            "goal_checks": [
                {"check_id": check.check_id, "kind": check.kind, "spec": check.spec}
                for check in state.goal_checks
            ],
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
