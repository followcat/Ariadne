"""Task mode activation policy and protocol helpers (Phase 14 hardening)."""

from __future__ import annotations

import pytest

from ariadne.errors import AriadneError
from ariadne.tasks.policy import resolve_task_mode
from ariadne.tasks.protocol import (
    parse_control_arguments,
    payload_has_tool,
    require_sole_control_call,
)
from ariadne.tasks.controller import SUBMIT_TASK_PLAN_NAME, REVISE_TASK_PLAN_NAME


def test_resolve_metadata_forces_on() -> None:
    on, reason = resolve_task_mode(
        policy="off", metadata={"task_mode": True}, has_active_task=False
    )
    assert on is True
    assert reason == "metadata_task_mode"


def test_resolve_metadata_forces_off() -> None:
    on, reason = resolve_task_mode(
        policy="on", metadata={"task_mode": False}, has_active_task=True
    )
    assert on is False
    assert reason == "metadata_task_mode_off"


def test_resolve_auto_resumes_active_task() -> None:
    on, reason = resolve_task_mode(
        policy="auto", metadata=None, has_active_task=True
    )
    assert on is True
    assert reason == "active_task_resume"


def test_resolve_auto_default_direct_loop() -> None:
    on, reason = resolve_task_mode(
        policy="auto", metadata={}, has_active_task=False
    )
    assert on is False
    assert reason == "policy_auto_default_off"


def test_resolve_policy_on_off() -> None:
    assert resolve_task_mode(policy="on", has_active_task=False)[0] is True
    assert resolve_task_mode(policy="off", has_active_task=True)[0] is False


def test_protocol_require_sole_submit() -> None:
    call = {
        "id": "c1",
        "function": {"name": SUBMIT_TASK_PLAN_NAME, "arguments": '{"goal":"g","steps":[]}'},
    }
    got = require_sole_control_call(
        [call],
        name=SUBMIT_TASK_PLAN_NAME,
        allow_when_task_exists=False,
        task_exists=False,
    )
    assert got["id"] == "c1"
    with pytest.raises(AriadneError) as ei:
        require_sole_control_call(
            [call],
            name=SUBMIT_TASK_PLAN_NAME,
            allow_when_task_exists=False,
            task_exists=True,
        )
    assert ei.value.error.code == "ARIADNE_TASK_PROTOCOL_ERROR"


def test_protocol_parse_args_and_payload_has() -> None:
    call = {
        "id": "c1",
        "function": {
            "name": REVISE_TASK_PLAN_NAME,
            "arguments": '{"goal":"g","steps":[{"intent":"i","done_when":[{"kind":"path_exists","path":"/workspace/a"}]}]}',
        },
    }
    args = parse_control_arguments(call)
    assert args["goal"] == "g"
    assert payload_has_tool([call], REVISE_TASK_PLAN_NAME)
    assert not payload_has_tool([call], SUBMIT_TASK_PLAN_NAME)
