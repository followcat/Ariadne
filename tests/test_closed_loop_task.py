from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory.facade import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tasks import DeterministicVerifier, SQLiteTaskStore, TaskController
from ariadne.tasks.controller import REVISE_TASK_PLAN_NAME, SUBMIT_TASK_PLAN_NAME
from ariadne.tasks.models import Check, TaskState
from ariadne.tools.registry import ToolContext, ToolRegistry, ToolSpec, build_default_registry
from ariadne.types import ToolCallTrace


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _app(tmp_path: Path, script) -> tuple[TurnApplication, Path, SQLiteTaskStore]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "data"
    memory = Memory.local(path=data / "memory")
    skills = SkillStore.from_dirs([], strict=False, user_root=data / "skills")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
    store = SQLiteTaskStore(data / "tasks.sqlite3")
    controller = TaskController(
        store=store,
        verifier=DeterministicVerifier(workspace),
    )
    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=data),
        task_controller=controller,
        guardrails_enabled=False,
        tool_loop_limit=10,
    )
    return app, workspace, store


def test_task_state_sqlite_revision_conflict(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    state = TaskState.from_plan(
        session_id="s1",
        user_id=None,
        goal="write a marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
            }
        ],
        workspace_fingerprint="tree-v1:test",
    )
    store.save(state, expected_revision=0)
    left = store.load(state.task_id)
    right = store.load(state.task_id)
    assert left is not None and right is not None
    left.goal = "updated once"
    store.save(left)
    right.goal = "stale update"
    with pytest.raises(AriadneError) as caught:
        store.save(right)
    assert caught.value.error.code == "ARIADNE_TASK_CONFLICT"


def test_deterministic_verifier_never_executes_command(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    verifier = DeterministicVerifier(workspace)
    check = Check.from_plan(
        {"kind": "command_exit", "spec": {"expected": 0}}
    )
    missing = verifier.run(check, traces=[], attempt_id="a1")
    assert missing.status == "not_run"
    stale = verifier.run(check, traces=[], attempt_id="resume", resume=True)
    assert stale.status == "stale"
    passed = verifier.run(
        check,
        traces=[
            ToolCallTrace(
                call_id="call-1",
                name="sandbox_exec",
                arguments={"cmd": "true"},
                output={"exit_code": 0, "timed_out": False},
            )
        ],
        attempt_id="a2",
    )
    assert passed.status == "pass"
    assert passed.evidence[0].ref == "call-1"


def test_resume_rechecks_current_step_against_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    controller = TaskController(store=store, verifier=DeterministicVerifier(workspace))
    state = TaskState.from_plan(
        session_id="resume-session",
        user_id=None,
        goal="create marker",
        steps=[
            {
                "intent": "create marker",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
                ],
            }
        ],
        workspace_fingerprint="before",
    )
    state = store.save(state, expected_revision=0)
    state, _, _ = controller.start_attempt(state)
    (workspace / "marker.txt").write_text("done\n", encoding="utf-8")

    resumed = controller.prepare_resume(store.load_active("resume-session"))  # type: ignore[arg-type]
    assert resumed.status == "completed"
    assert resumed.steps[0].status == "verified"
    assert resumed.steps[0].check_results[0].status == "pass"
    assert store.load_active("resume-session") is None


def test_failed_precondition_stops_before_attempt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    controller = TaskController(store=store, verifier=DeterministicVerifier(workspace))
    state = TaskState.from_plan(
        session_id="precondition-session",
        user_id=None,
        goal="edit an existing file",
        steps=[
            {
                "intent": "edit marker",
                "preconditions": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
                ],
                "done_when": [
                    {
                        "kind": "file_contains",
                        "spec": {"path": "/workspace/marker.txt", "text": "updated"},
                    }
                ],
            }
        ],
        workspace_fingerprint="before",
    )
    state = store.save(state, expected_revision=0)
    with pytest.raises(AriadneError) as caught:
        controller.start_attempt(state)
    assert caught.value.error.code == "ARIADNE_TASK_PRECONDITION_FAILED"
    active = store.load_active("precondition-session")
    assert active is not None and active.status == "needs_input"
    assert active.steps[0].attempt == 0


def test_task_mode_plan_act_verify_and_complete(tmp_path: Path) -> None:
    step = {"n": 0}

    def script(messages, model_tools):
        n = step["n"]
        step["n"] += 1
        names = {(tool.get("function") or {}).get("name") for tool in (model_tools or [])}
        if n == 0:
            assert names == {SUBMIT_TASK_PLAN_NAME}
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        SUBMIT_TASK_PLAN_NAME,
                        {
                            "goal": "create and verify result.txt",
                            "steps": [
                                {
                                    "intent": "write the result",
                                    "done_when": [
                                        {
                                            "kind": "file_contains",
                                            "spec": {
                                                "path": "/workspace/result.txt",
                                                "text": "closed loop",
                                            },
                                        }
                                    ],
                                    "failure_policy": "ask_user",
                                },
                                {
                                    "intent": "run a deterministic check",
                                    "done_when": [
                                        {"kind": "command_exit", "spec": {"expected": 0}}
                                    ],
                                    "failure_policy": "abort",
                                },
                            ],
                            "goal_checks": [
                                {
                                    "kind": "file_contains",
                                    "spec": {
                                        "path": "/workspace/result.txt",
                                        "text": "closed loop",
                                    },
                                }
                            ],
                        },
                        "plan-1",
                    )
                ],
            }
        if n == 1:
            assert "sandbox_write_file" in names
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/result.txt", "content": "closed loop\n"},
                        "write-1",
                    )
                ],
            }
        if n == 2:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_exec",
                        {"cmd": "test -f result.txt", "cwd": "/workspace"},
                        "test-1",
                    )
                ],
            }
        assert model_tools is None
        return {"content": "Created result.txt and verified it with evidence."}

    app, workspace, store = _app(tmp_path, script)

    async def run():
        events = []
        async for event in app.run_events(
            prompt="create a verified result",
            session_id="task-e2e",
            metadata={"task_mode": True},
        ):
            events.append(event)
        return events

    events = asyncio.run(run())
    result = next(
        event.data["result"] for event in events if event.kind == "turn_completed"
    )
    assert result.status == "completed"
    assert result.task is not None and result.task.verified_steps == 2
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "closed loop\n"
    assert store.load_active("task-e2e") is None
    kinds = [event.kind for event in events]
    assert kinds.count("task_check_completed") == 2
    assert "task_started" in kinds
    assert "task_completed" in kinds
    assert all(trace.step_id for trace in result.tool_calls)
    assert result.tool_calls[0].step_id != result.tool_calls[1].step_id
    assert result.tool_calls[0].attempt_id.endswith(":1")


def test_failed_check_replans_from_cited_evidence(tmp_path: Path) -> None:
    step = {"n": 0}
    store_box: dict[str, SQLiteTaskStore] = {}

    def script(messages, model_tools):
        n = step["n"]
        step["n"] += 1
        names = {(tool.get("function") or {}).get("name") for tool in (model_tools or [])}
        if n == 0:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        SUBMIT_TASK_PLAN_NAME,
                        {
                            "goal": "write good content",
                            "steps": [
                                {
                                    "intent": "first attempt",
                                    "done_when": [
                                        {
                                            "kind": "file_contains",
                                            "spec": {
                                                "path": "/workspace/value.txt",
                                                "text": "good",
                                            },
                                        }
                                    ],
                                    "failure_policy": "replan",
                                }
                            ],
                        },
                        "plan-1",
                    )
                ],
            }
        if n == 1:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/value.txt", "content": "bad\n"},
                        "write-bad",
                    )
                ],
            }
        if n == 2:
            assert names == {REVISE_TASK_PLAN_NAME}
            state = store_box["store"].load_active("replan-session")
            assert state is not None and state.replan_required
            evidence_id = state.replan_evidence[0].evidence_id
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        REVISE_TASK_PLAN_NAME,
                        {
                            "reason": "file content check failed",
                            "evidence_ids": [evidence_id],
                            "steps": [
                                {
                                    "intent": "write corrected content",
                                    "done_when": [
                                        {
                                            "kind": "file_contains",
                                            "spec": {
                                                "path": "/workspace/value.txt",
                                                "text": "good",
                                            },
                                        }
                                    ],
                                    "failure_policy": "abort",
                                }
                            ],
                        },
                        "replan-1",
                    )
                ],
            }
        if n == 3:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/value.txt", "content": "good\n"},
                        "write-good",
                    )
                ],
            }
        assert model_tools is None
        return {"content": "Corrected and verified."}

    app, workspace, store = _app(tmp_path, script)
    store_box["store"] = store
    events = []

    async def run():
        async for event in app.run_events(
            prompt="write good content",
            session_id="replan-session",
            metadata={"task_mode": True},
        ):
            events.append(event)

    asyncio.run(run())
    result = next(
        event.data["result"] for event in events if event.kind == "turn_completed"
    )
    assert result.status == "completed"
    assert (workspace / "value.txt").read_text(encoding="utf-8") == "good\n"
    tasks = store.list_for_session("replan-session")
    assert len(tasks) == 1
    state = tasks[0]
    assert [item.status for item in state.steps] == ["failed", "verified"]
    assert len(state.plan_revisions) == 1
    assert state.plan_revisions[0].reason == "file content check failed"
    assert state.plan_revisions[0].evidence
    assert "task_replanned" in [event.kind for event in events]


def test_goal_check_blocks_false_completion(tmp_path: Path) -> None:
    step = {"n": 0}

    def script(messages, model_tools):
        n = step["n"]
        step["n"] += 1
        if n == 0:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        SUBMIT_TASK_PLAN_NAME,
                        {
                            "goal": "write expected content",
                            "steps": [
                                {
                                    "intent": "create file",
                                    "done_when": [
                                        {
                                            "kind": "path_exists",
                                            "spec": {"path": "/workspace/value.txt"},
                                        }
                                    ],
                                }
                            ],
                            "goal_checks": [
                                {
                                    "kind": "file_contains",
                                    "spec": {
                                        "path": "/workspace/value.txt",
                                        "text": "expected",
                                    },
                                }
                            ],
                        },
                        "plan-1",
                    )
                ],
            }
        if n == 1:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/value.txt", "content": "wrong\n"},
                        "write-1",
                    )
                ],
            }
        return {"content": "The file exists, but the requested content is not verified."}

    app, _, store = _app(tmp_path, script)
    result = asyncio.run(
        app.run(
            prompt="write expected content",
            session_id="goal-check",
            metadata={"task_mode": True},
        )
    )
    assert result.status == "needs_input"
    state = store.load_active("goal-check")
    assert state is not None
    assert state.steps[0].status == "verified"
    assert state.goal_check_results[0].status == "fail"


def test_retry_requires_read_or_explicit_idempotency(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    controller = TaskController(store=store, verifier=DeterministicVerifier(workspace))
    state = TaskState.from_plan(
        session_id="retry-session",
        user_id=None,
        goal="write marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
                ],
                "failure_policy": "retry",
                "max_retries": 2,
            }
        ],
        workspace_fingerprint="before",
    )
    state = store.save(state, expected_revision=0)
    state, _, attempt_id = controller.start_attempt(state)

    async def handler(args, ctx):
        return args

    unsafe = ToolSpec(
        name="unsafe_write",
        description="write",
        parameters={"type": "object"},
        handler=handler,
        side_effect_level="write",
        idempotent=False,
    )
    outcome = controller.record_attempt(
        state,
        traces=[
            ToolCallTrace(
                call_id="write-1",
                name="unsafe_write",
                arguments={},
                output={"ok": True},
            )
        ],
        spec=unsafe,
        effect_level="write",
        attempt_id=attempt_id,
    )
    assert outcome.safe_retry is False
    assert outcome.state.status == "needs_input"
    assert "unsafe" in outcome.state.open_questions[0].prompt.lower()

    safe_state = TaskState.from_plan(
        session_id="safe-retry-session",
        user_id=None,
        goal="write safe marker",
        steps=[
            {
                "intent": "idempotent write",
                "done_when": [
                    {
                        "kind": "path_exists",
                        "spec": {"path": "/workspace/safe-marker.txt"},
                    }
                ],
                "failure_policy": "retry",
                "max_retries": 1,
            }
        ],
        workspace_fingerprint="before",
    )
    safe_state = store.save(safe_state, expected_revision=0)
    safe_state, _, first_attempt = controller.start_attempt(safe_state)
    idempotent = ToolSpec(
        name="idempotent_write",
        description="write",
        parameters={"type": "object"},
        handler=handler,
        side_effect_level="write",
        idempotent=True,
    )
    first = controller.record_attempt(
        safe_state,
        traces=[
            ToolCallTrace(
                call_id="safe-write-1",
                name="idempotent_write",
                arguments={},
                output={"ok": True},
            )
        ],
        spec=idempotent,
        effect_level="write",
        attempt_id=first_attempt,
    )
    assert first.safe_retry is True and first.state.status == "active"
    (workspace / "safe-marker.txt").write_text("done\n", encoding="utf-8")
    safe_state, _, second_attempt = controller.start_attempt(first.state)
    second = controller.record_attempt(
        safe_state,
        traces=[
            ToolCallTrace(
                call_id="safe-write-2",
                name="idempotent_write",
                arguments={},
                output={"ok": True},
            )
        ],
        spec=idempotent,
        effect_level="write",
        attempt_id=second_attempt,
    )
    assert second.state.status == "completed"


def test_unverified_task_answer_becomes_needs_input(tmp_path: Path) -> None:
    step = {"n": 0}

    def script(messages, model_tools):
        n = step["n"]
        step["n"] += 1
        if n == 0:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        SUBMIT_TASK_PLAN_NAME,
                        {
                            "goal": "create marker",
                            "steps": [
                                {
                                    "intent": "create marker",
                                    "done_when": [
                                        {
                                            "kind": "path_exists",
                                            "spec": {"path": "/workspace/marker.txt"},
                                        }
                                    ],
                                }
                            ],
                        },
                        "plan-1",
                    )
                ],
            }
        return {"content": "I think it is done."}

    app, _, store = _app(tmp_path, script)
    result = asyncio.run(
        app.run(
            prompt="make a marker",
            session_id="needs-input",
            metadata={"task_mode": True},
        )
    )
    assert result.status == "needs_input"
    assert result.task is not None and result.task.status == "needs_input"
    active = store.load_active("needs-input")
    assert active is not None and active.open_questions[0].prompt == "I think it is done."


def test_tool_registry_uses_full_json_schema(tmp_path: Path) -> None:
    async def handler(args, ctx):
        return args

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="strict_tool",
            description="strict",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["count"],
                "properties": {"count": {"type": "integer", "minimum": 1}},
            },
            handler=handler,
        )
    )
    context = ToolContext(session_id="s", turn_id="t", sandbox=None)
    with pytest.raises(AriadneError) as caught:
        asyncio.run(registry.invoke("strict_tool", {"count": "one"}, context))
    assert caught.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"
    assert caught.value.error.details["validator"] == "type"


def test_tool_registry_enforces_required_credentials() -> None:
    async def handler(args, ctx):
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="credentialed",
            description="credentialed",
            parameters={"type": "object", "additionalProperties": False},
            handler=handler,
            required_credentials=("service.token",),
            side_effect_level="read",
        )
    )
    missing = ToolContext(session_id="s", turn_id="t", sandbox=None)
    with pytest.raises(AriadneError) as caught:
        asyncio.run(registry.invoke("credentialed", {}, missing))
    assert caught.value.error.code == "ARIADNE_TOOL_CREDENTIALS_MISSING"
    allowed = ToolContext(
        session_id="s",
        turn_id="t",
        sandbox=None,
        available_credentials=frozenset({"service.token"}),
    )
    assert asyncio.run(registry.invoke("credentialed", {}, allowed)) == {"ok": True}
