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
from ariadne.tasks.controller import SUBMIT_TASK_PLAN_NAME
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
