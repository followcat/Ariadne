"""Unit tests for tasks.runtime helpers (tools payload, plan control, final status)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.tasks.controller import (
    REVISE_TASK_PLAN_NAME,
    REVISE_TASK_PLAN_TOOL,
    SUBMIT_TASK_PLAN_NAME,
    SUBMIT_TASK_PLAN_TOOL,
    TaskController,
)
from ariadne.tasks.runtime import (
    apply_submit_task_plan,
    bootstrap_task_session,
    resolve_final_answer_status,
    select_task_tools_payload,
)
from ariadne.tasks.store import SQLiteTaskStore
from ariadne.tasks.verify import DeterministicVerifier


def _controller(tmp_path: Path) -> TaskController:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return TaskController(
        store=SQLiteTaskStore(tmp_path / "tasks.sqlite3"),
        verifier=DeterministicVerifier(ws),
    )


def _minimal_plan_args(goal: str = "g") -> dict:
    return {
        "goal": goal,
        "goal_checks": [
            {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}},
        ],
        "steps": [
            {
                "intent": "write",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}},
                ],
            }
        ],
    }


def test_select_task_tools_payload_phases() -> None:
    exposure = [{"type": "function", "function": {"name": "sandbox_write_file"}}]
    assert select_task_tools_payload(task_state=None, exposure_tools=exposure) == [
        SUBMIT_TASK_PLAN_TOOL
    ]

    class _S:
        replan_required = False
        status = "active"

    assert (
        select_task_tools_payload(task_state=_S(), exposure_tools=exposure) is exposure
    )

    class _Replan:
        replan_required = True
        status = "active"

    assert select_task_tools_payload(task_state=_Replan(), exposure_tools=exposure) == [
        REVISE_TASK_PLAN_TOOL
    ]

    for st in ("needs_input", "completed", "failed", "cancelled"):
        class _Term:
            replan_required = False
            status = st

        assert (
            select_task_tools_payload(task_state=_Term(), exposure_tools=exposure)
            is None
        )


def test_apply_submit_and_bootstrap(tmp_path: Path) -> None:
    ctl = _controller(tmp_path)
    call = {
        "id": "plan-1",
        "type": "function",
        "function": {
            "name": SUBMIT_TASK_PLAN_NAME,
            "arguments": __import__("json").dumps(_minimal_plan_args("make marker")),
        },
    }
    result = apply_submit_task_plan(
        controller=ctl,
        tool_calls_payload=[call],
        task_state=None,
        session_id="s1",
        user_id="u1",
        original_user_goal="make marker",
        task_mode_reason="metadata_task_mode",
    )
    assert result.state.goal == "make marker"
    assert result.state.status == "active"
    assert len(result.appends) == 2
    assert result.events[0].kind == "task_started"
    assert result.events[0].data["task_mode_reason"] == "metadata_task_mode"

    boot = bootstrap_task_session(
        controller=ctl,
        active=result.state,
        prompt="continue",
        task_mode_reason="active_task_resume",
    )
    assert boot.state is not None
    assert boot.events[0].kind == "task_resumed"
    assert boot.events[0].data["task_mode_reason"] == "active_task_resume"

    # second submit while task exists is protocol error
    with pytest.raises(AriadneError) as ei:
        apply_submit_task_plan(
            controller=ctl,
            tool_calls_payload=[call],
            task_state=result.state,
            session_id="s1",
            user_id="u1",
            original_user_goal="make marker",
            task_mode_reason="metadata_task_mode",
        )
    assert ei.value.error.code == "ARIADNE_TASK_PROTOCOL_ERROR"


def test_resolve_final_answer_requires_plan(tmp_path: Path) -> None:
    ctl = _controller(tmp_path)
    status, err, state, ev = resolve_final_answer_status(
        controller=ctl, state=None, assistant_text="done"
    )
    assert status == "failed"
    assert err is not None
    assert err.code == "ARIADNE_TASK_PLAN_REQUIRED"
    assert state is None
    assert ev is None


def test_resolve_final_answer_completed(tmp_path: Path) -> None:
    ctl = _controller(tmp_path)
    state = ctl.create_from_plan(
        session_id="s2",
        user_id="u",
        original_user_goal="g",
        arguments=_minimal_plan_args(),
    )
    state.status = "completed"
    status, err, out, ev = resolve_final_answer_status(
        controller=ctl, state=state, assistant_text="ok"
    )
    assert status == "completed"
    assert err is None
    assert out is state
    assert ev is None


def test_submit_tool_name_constant() -> None:
    # Keep control names stable for UI / docs
    assert SUBMIT_TASK_PLAN_NAME == "submit_task_plan"
    assert REVISE_TASK_PLAN_NAME == "revise_task_plan"
