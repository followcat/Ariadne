from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, cast

from ..errors import AriadneError, app_error
from ..types import AppError

TaskStatus = Literal["active", "needs_input", "completed", "failed", "cancelled"]
StepStatus = Literal["pending", "running", "verified", "failed", "skipped"]
CheckStatus = Literal["pass", "fail", "error", "stale", "not_run"]
FailurePolicy = Literal["retry", "replan", "ask_user", "abort"]
CheckKind = Literal[
    "command_exit",
    "path_exists",
    "path_absent",
    "file_contains",
    "git_diff_matches",
    "http_response",
    "json_path",
    "llm_semantic",
    "image_file",
    "user_confirm",
]

TASK_SCHEMA_VERSION = 2
PHASE_14A_CHECK_KINDS = frozenset(
    {"command_exit", "path_exists", "path_absent", "file_contains"}
)
PHASE_14E_CHECK_KINDS = PHASE_14A_CHECK_KINDS | {"llm_semantic", "image_file"}
ALL_CHECK_KINDS = frozenset(
    {
        "command_exit",
        "path_exists",
        "path_absent",
        "file_contains",
        "git_diff_matches",
        "http_response",
        "json_path",
        "llm_semantic",
        "image_file",
        "user_confirm",
    }
)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AriadneError(
            app_error("ARIADNE_TASK_INVALID", f"{label} must be an object", field=label)
        )
    return cast(dict[str, Any], value)


def _one_of(value: Any, *, label: str, allowed: set[str]) -> str:
    text = str(value or "")
    if text not in allowed:
        raise AriadneError(
            app_error(
                "ARIADNE_TASK_INVALID",
                f"{label} has invalid value: {text!r}",
                field=label,
                allowed=sorted(allowed),
            )
        )
    return text


@dataclass(slots=True)
class EvidenceRef:
    evidence_id: str
    kind: Literal[
        "tool_result", "command", "file_diff", "test_log", "memory_hit", "user", "image"
    ]
    ref: str
    summary: str
    attempt_id: str
    at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceRef":
        kind = _one_of(
            value.get("kind"),
            label="evidence.kind",
            allowed={
                "tool_result",
                "command",
                "file_diff",
                "test_log",
                "memory_hit",
                "user",
                "image",
            },
        )
        return cls(
            evidence_id=str(value["evidence_id"]),
            kind=cast(Any, kind),
            ref=str(value["ref"]),
            summary=str(value.get("summary") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            at=float(value.get("at") or 0.0),
        )


@dataclass(slots=True)
class Check:
    check_id: str
    kind: CheckKind
    spec: dict[str, Any]
    required: bool = True

    @classmethod
    def from_plan(
        cls,
        value: dict[str, Any],
        *,
        allowed_kinds: frozenset[str] = PHASE_14E_CHECK_KINDS,
    ) -> "Check":
        raw = _mapping(value, label="check")
        kind = str(raw.get("kind") or "")
        if kind not in allowed_kinds:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    f"unsupported check kind: {kind!r}",
                    kind=kind,
                    allowed=sorted(allowed_kinds),
                )
            )
        spec = _mapping(raw.get("spec", {}), label="check.spec")
        return cls(
            check_id=_new_id("check"),
            kind=cast(CheckKind, kind),
            spec=dict(spec),
            required=bool(raw.get("required", True)),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Check":
        kind = str(value.get("kind") or "")
        if kind not in ALL_CHECK_KINDS:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", f"stored check kind is invalid: {kind!r}")
            )
        return cls(
            check_id=str(value["check_id"]),
            kind=cast(CheckKind, kind),
            spec=dict(_mapping(value.get("spec", {}), label="check.spec")),
            required=bool(value.get("required", True)),
        )


@dataclass(slots=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    evidence: list[EvidenceRef] = field(default_factory=list)
    observed_value: Any = None
    error: AppError | None = None
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.error is not None:
            out["error"] = asdict(self.error)
        return out

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CheckResult":
        raw_error = value.get("error")
        error = None
        if isinstance(raw_error, dict):
            error = AppError(
                code=str(raw_error.get("code") or "ARIADNE_TASK_CHECK_ERROR"),
                message=str(raw_error.get("message") or "check failed"),
                details=dict(raw_error.get("details") or {}),
                retriable=bool(raw_error.get("retriable", False)),
            )
        status = _one_of(
            value.get("status"),
            label="check_result.status",
            allowed={"pass", "fail", "error", "stale", "not_run"},
        )
        return cls(
            check_id=str(value["check_id"]),
            status=cast(CheckStatus, status),
            evidence=[EvidenceRef.from_dict(v) for v in value.get("evidence", [])],
            observed_value=value.get("observed_value"),
            error=error,
            checked_at=float(value.get("checked_at") or 0.0),
        )


@dataclass(slots=True)
class Observation:
    observation_id: str
    kind: Literal["tool_result", "check_result", "user", "environment"]
    summary: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Observation":
        kind = _one_of(
            value.get("kind"),
            label="observation.kind",
            allowed={"tool_result", "check_result", "user", "environment"},
        )
        return cls(
            observation_id=str(value["observation_id"]),
            kind=cast(Any, kind),
            summary=str(value.get("summary") or ""),
            evidence=[EvidenceRef.from_dict(v) for v in value.get("evidence", [])],
            at=float(value.get("at") or 0.0),
        )


@dataclass(slots=True)
class Assumption:
    assumption_id: str
    text: str
    status: Literal["current", "stale", "invalid"] = "current"
    recheck: Check | None = None
    checked_at: float | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Assumption":
        raw_check = value.get("recheck")
        status = _one_of(
            value.get("status"),
            label="assumption.status",
            allowed={"current", "stale", "invalid"},
        )
        return cls(
            assumption_id=str(value["assumption_id"]),
            text=str(value.get("text") or ""),
            status=cast(Any, status),
            recheck=Check.from_dict(raw_check) if isinstance(raw_check, dict) else None,
            checked_at=(float(value["checked_at"]) if value.get("checked_at") is not None else None),
        )


@dataclass(slots=True)
class OpenQuestion:
    question_id: str
    prompt: str
    asked_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "OpenQuestion":
        return cls(
            question_id=str(value["question_id"]),
            prompt=str(value.get("prompt") or ""),
            asked_at=float(value.get("asked_at") or 0.0),
        )


@dataclass(slots=True)
class PlanRevision:
    revision: int
    reason: str
    evidence: list[EvidenceRef]
    prior_step_ids: list[str]
    new_step_ids: list[str]
    at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlanRevision":
        return cls(
            revision=int(value.get("revision") or 0),
            reason=str(value.get("reason") or ""),
            evidence=[EvidenceRef.from_dict(v) for v in value.get("evidence", [])],
            prior_step_ids=[str(v) for v in value.get("prior_step_ids", [])],
            new_step_ids=[str(v) for v in value.get("new_step_ids", [])],
            at=float(value.get("at") or 0.0),
        )


@dataclass(slots=True)
class Step:
    step_id: str
    intent: str
    status: StepStatus = "pending"
    preconditions: list[Check] = field(default_factory=list)
    done_when: list[Check] = field(default_factory=list)
    tools_hint: list[str] = field(default_factory=list)
    max_retries: int = 0
    failure_policy: FailurePolicy = "ask_user"
    evidence: list[EvidenceRef] = field(default_factory=list)
    attempt: int = 0
    precondition_results: list[CheckResult] = field(default_factory=list)
    check_results: list[CheckResult] = field(default_factory=list)

    @classmethod
    def from_plan(cls, value: dict[str, Any]) -> "Step":
        raw = _mapping(value, label="step")
        intent = str(raw.get("intent") or "").strip()
        if not intent:
            raise AriadneError(app_error("ARIADNE_TASK_INVALID", "step.intent is required"))
        done_raw = raw.get("done_when")
        if not isinstance(done_raw, list) or not done_raw:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "every step needs non-empty done_when")
            )
        pre_raw = raw.get("preconditions", [])
        if not isinstance(pre_raw, list):
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "step.preconditions must be an array")
            )
        policy = str(raw.get("failure_policy") or "ask_user")
        if policy not in {"retry", "replan", "ask_user", "abort"}:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", f"invalid failure_policy: {policy!r}")
            )
        retries = int(raw.get("max_retries") or 0)
        if retries < 0 or retries > 3:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "max_retries must be between 0 and 3")
            )
        hints = raw.get("tools_hint", [])
        if not isinstance(hints, list) or any(not isinstance(v, str) for v in hints):
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "step.tools_hint must be an array of strings")
            )
        done_when = [Check.from_plan(v) for v in done_raw]
        if not any(check.required for check in done_when):
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    "every step needs at least one required done_when check",
                )
            )
        return cls(
            step_id=_new_id("step"),
            intent=intent,
            preconditions=[Check.from_plan(v) for v in pre_raw],
            done_when=done_when,
            tools_hint=[str(v) for v in hints],
            max_retries=retries,
            failure_policy=cast(FailurePolicy, policy),
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["precondition_results"] = [
            result.to_dict() for result in self.precondition_results
        ]
        out["check_results"] = [result.to_dict() for result in self.check_results]
        return out

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Step":
        status = _one_of(
            value.get("status"),
            label="step.status",
            allowed={"pending", "running", "verified", "failed", "skipped"},
        )
        failure_policy = _one_of(
            value.get("failure_policy"),
            label="step.failure_policy",
            allowed={"retry", "replan", "ask_user", "abort"},
        )
        return cls(
            step_id=str(value["step_id"]),
            intent=str(value.get("intent") or ""),
            status=cast(StepStatus, status),
            preconditions=[Check.from_dict(v) for v in value.get("preconditions", [])],
            done_when=[Check.from_dict(v) for v in value.get("done_when", [])],
            tools_hint=[str(v) for v in value.get("tools_hint", [])],
            max_retries=int(value.get("max_retries") or 0),
            failure_policy=cast(FailurePolicy, failure_policy),
            evidence=[EvidenceRef.from_dict(v) for v in value.get("evidence", [])],
            attempt=int(value.get("attempt") or 0),
            precondition_results=[
                CheckResult.from_dict(v) for v in value.get("precondition_results", [])
            ],
            check_results=[CheckResult.from_dict(v) for v in value.get("check_results", [])],
        )


@dataclass(slots=True)
class TaskState:
    task_id: str
    session_id: str
    user_id: str | None
    original_user_goal: str
    goal: str
    steps: list[Step]
    schema_version: int = TASK_SCHEMA_VERSION
    revision: int = 0
    status: TaskStatus = "active"
    goal_checks: list[Check] = field(default_factory=list)
    goal_check_results: list[CheckResult] = field(default_factory=list)
    current_step_id: str | None = None
    last_observation: Observation | None = None
    open_questions: list[OpenQuestion] = field(default_factory=list)
    workspace_fingerprint: str = ""
    assumptions: list[Assumption] = field(default_factory=list)
    plan_revisions: list[PlanRevision] = field(default_factory=list)
    replan_required: bool = False
    replan_reason: str = ""
    replan_evidence: list[EvidenceRef] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_plan(
        cls,
        *,
        session_id: str,
        user_id: str | None,
        original_user_goal: str,
        goal: str,
        steps: list[dict[str, Any]],
        workspace_fingerprint: str,
        goal_checks: list[dict[str, Any]] | None = None,
    ) -> "TaskState":
        clean_original_goal = str(original_user_goal or "").strip()
        clean_goal = str(goal or "").strip()
        if not clean_original_goal:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "original user goal is required")
            )
        if not clean_goal:
            raise AriadneError(app_error("ARIADNE_TASK_INVALID", "task goal is required"))
        if not steps:
            raise AriadneError(app_error("ARIADNE_TASK_INVALID", "task needs at least one step"))
        parsed_steps = [Step.from_plan(v) for v in steps]
        parsed_goal_checks = [Check.from_plan(v) for v in (goal_checks or [])]
        if not parsed_goal_checks:
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "task needs at least one goal check")
            )
        if not any(check.required for check in parsed_goal_checks):
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    "task needs at least one required goal check",
                )
            )
        return cls(
            task_id=_new_id("task"),
            session_id=session_id,
            user_id=user_id,
            original_user_goal=clean_original_goal,
            goal=clean_goal,
            steps=parsed_steps,
            goal_checks=parsed_goal_checks,
            current_step_id=parsed_steps[0].step_id,
            workspace_fingerprint=workspace_fingerprint,
        )

    @property
    def current_step(self) -> Step | None:
        if self.current_step_id is None:
            return None
        return next((step for step in self.steps if step.step_id == self.current_step_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "status": self.status,
            "original_user_goal": self.original_user_goal,
            "goal": self.goal,
            "goal_checks": [asdict(v) for v in self.goal_checks],
            "goal_check_results": [result.to_dict() for result in self.goal_check_results],
            "steps": [v.to_dict() for v in self.steps],
            "current_step_id": self.current_step_id,
            "last_observation": asdict(self.last_observation) if self.last_observation else None,
            "open_questions": [asdict(v) for v in self.open_questions],
            "workspace_fingerprint": self.workspace_fingerprint,
            "assumptions": [asdict(v) for v in self.assumptions],
            "plan_revisions": [asdict(v) for v in self.plan_revisions],
            "replan_required": self.replan_required,
            "replan_reason": self.replan_reason,
            "replan_evidence": [asdict(v) for v in self.replan_evidence],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TaskState":
        version = int(value.get("schema_version") or 0)
        if version == 1:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_SCHEMA_MIGRATION_REQUIRED",
                    "TaskState v1 cannot be safely upgraded because it has no "
                    "authoritative original user goal or required goal oracle; "
                    "explicitly abandon or export the legacy task before continuing",
                    expected=TASK_SCHEMA_VERSION,
                    actual=version,
                )
            )
        if version != TASK_SCHEMA_VERSION:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_SCHEMA_UNSUPPORTED",
                    f"unsupported task schema version: {version}",
                    expected=TASK_SCHEMA_VERSION,
                    actual=version,
                )
            )
        raw_observation = value.get("last_observation")
        status = _one_of(
            value.get("status"),
            label="task.status",
            allowed={"active", "needs_input", "completed", "failed", "cancelled"},
        )
        state = cls(
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            user_id=(str(value["user_id"]) if value.get("user_id") is not None else None),
            original_user_goal=str(value.get("original_user_goal") or ""),
            goal=str(value.get("goal") or ""),
            steps=[Step.from_dict(v) for v in value.get("steps", [])],
            schema_version=version,
            revision=int(value.get("revision") or 0),
            status=cast(TaskStatus, status),
            goal_checks=[Check.from_dict(v) for v in value.get("goal_checks", [])],
            goal_check_results=[
                CheckResult.from_dict(v) for v in value.get("goal_check_results", [])
            ],
            current_step_id=(str(value["current_step_id"]) if value.get("current_step_id") else None),
            last_observation=(
                Observation.from_dict(raw_observation)
                if isinstance(raw_observation, dict)
                else None
            ),
            open_questions=[OpenQuestion.from_dict(v) for v in value.get("open_questions", [])],
            workspace_fingerprint=str(value.get("workspace_fingerprint") or ""),
            assumptions=[Assumption.from_dict(v) for v in value.get("assumptions", [])],
            plan_revisions=[PlanRevision.from_dict(v) for v in value.get("plan_revisions", [])],
            replan_required=bool(value.get("replan_required", False)),
            replan_reason=str(value.get("replan_reason") or ""),
            replan_evidence=[
                EvidenceRef.from_dict(v) for v in value.get("replan_evidence", [])
            ],
            created_at=float(value.get("created_at") or 0.0),
            updated_at=float(value.get("updated_at") or 0.0),
        )
        if not state.steps:
            raise AriadneError(app_error("ARIADNE_TASK_INVALID", "stored task has no steps"))
        if not state.goal_checks or not any(check.required for check in state.goal_checks):
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    "stored task has no required goal check",
                )
            )
        if any(
            not step.done_when or not any(check.required for check in step.done_when)
            for step in state.steps
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    "stored task step has no required done_when check",
                )
            )
        if not state.original_user_goal.strip():
            raise AriadneError(
                app_error("ARIADNE_TASK_INVALID", "stored task has no original user goal")
            )
        if state.current_step_id is not None and state.current_step is None:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_INVALID",
                    "stored current_step_id does not reference a task step",
                    current_step_id=state.current_step_id,
                )
            )
        return state


@dataclass(slots=True)
class TaskSummary:
    task_id: str
    status: TaskStatus
    goal: str
    current_step_id: str | None
    revision: int
    verified_steps: int
    total_steps: int
    plan_revisions: int

    @classmethod
    def from_state(cls, state: TaskState) -> "TaskSummary":
        return cls(
            task_id=state.task_id,
            status=state.status,
            goal=state.goal,
            current_step_id=state.current_step_id,
            revision=state.revision,
            verified_steps=sum(step.status == "verified" for step in state.steps),
            total_steps=len(state.steps),
            plan_revisions=len(state.plan_revisions),
        )
