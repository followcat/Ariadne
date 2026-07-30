from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory.facade import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.outcomes import SkillOutcomeLedger
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
        original_user_goal="write a marker",
        goal="write a marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
            }
        ],
        goal_checks=[{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
        workspace_fingerprint="tree-v1:test",
    )
    store.save(state, expected_revision=0)
    left = store.load(state.task_id)
    right = store.load(state.task_id)
    assert left is not None and right is not None
    left.workspace_fingerprint = "updated once"
    store.save(left)
    history = store.event_history(state.task_id)
    assert [item["revision"] for item in history] == [1, 2]
    assert history[1]["previous_digest"] == history[0]["event_digest"]
    assert store.verify_event_history(state.task_id) is True
    original = store.load_revision(state.task_id, 1)
    assert original is not None and original.workspace_fingerprint == "tree-v1:test"
    right.workspace_fingerprint = "stale update"
    with pytest.raises(AriadneError) as caught:
        store.save(right)
    assert caught.value.error.code == "ARIADNE_TASK_CONFLICT"

    current = store.load(state.task_id)
    assert current is not None
    current.original_user_goal = "weakened"
    current.goal = "weakened"
    with pytest.raises(AriadneError) as immutable:
        store.save(current)
    assert immutable.value.error.code == "ARIADNE_TASK_GOAL_IMMUTABLE"


@pytest.mark.parametrize("tamper", ["payload", "revision"])
def test_task_store_rejects_snapshot_that_diverges_from_event_tail(
    tmp_path: Path, tamper: str
) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    state = TaskState.from_plan(
        session_id=f"audit-{tamper}",
        user_id=None,
        original_user_goal="write a marker",
        goal="write a marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
            }
        ],
        goal_checks=[{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
        workspace_fingerprint="before",
    )
    store.save(state, expected_revision=0)

    with sqlite3.connect(store.path) as con:
        if tamper == "payload":
            raw = con.execute(
                "SELECT payload FROM tasks WHERE task_id=?", (state.task_id,)
            ).fetchone()[0]
            payload = json.loads(raw)
            payload["workspace_fingerprint"] = "tampered"
            con.execute(
                "UPDATE tasks SET payload=? WHERE task_id=?",
                (json.dumps(payload, separators=(",", ":")), state.task_id),
            )
        else:
            con.execute(
                "UPDATE tasks SET revision=revision+1 WHERE task_id=?",
                (state.task_id,),
            )

    assert store.verify_event_history(state.task_id) is False
    load_calls = [
        lambda: store.load(state.task_id),
        lambda: store.load_active(state.session_id),
        lambda: store.list_for_session(state.session_id),
        lambda: store.load_revision(state.task_id, 1),
        lambda: store.save(state),
    ]
    for load_call in load_calls:
        with pytest.raises(AriadneError) as caught:
            load_call()
        assert caught.value.error.code == "ARIADNE_TASK_AUDIT_MISMATCH"


def test_task_store_rejects_event_revision_gap(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    state = TaskState.from_plan(
        session_id="audit-gap",
        user_id=None,
        original_user_goal="write a marker",
        goal="write a marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
            }
        ],
        goal_checks=[{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
        workspace_fingerprint="before",
    )
    store.save(state, expected_revision=0)
    state.workspace_fingerprint = "after"
    store.save(state)
    with sqlite3.connect(store.path) as con:
        con.execute(
            "DELETE FROM task_events WHERE task_id=? AND revision=1",
            (state.task_id,),
        )

    assert store.verify_event_history(state.task_id) is False
    with pytest.raises(AriadneError) as caught:
        store.load(state.task_id)
    assert caught.value.error.code == "ARIADNE_TASK_AUDIT_MISMATCH"


def test_task_state_v1_requires_explicit_incompatible_upgrade_handling(
    tmp_path: Path,
) -> None:
    state = TaskState.from_plan(
        session_id="legacy-task",
        user_id=None,
        original_user_goal="write a marker",
        goal="write a marker",
        steps=[
            {
                "intent": "write marker",
                "done_when": [{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
            }
        ],
        goal_checks=[{"kind": "path_exists", "spec": {"path": "marker.txt"}}],
        workspace_fingerprint="before",
    )
    payload = state.to_dict()
    payload["schema_version"] = 1
    payload["revision"] = 1
    payload.pop("original_user_goal")

    with pytest.raises(AriadneError) as caught:
        TaskState.from_dict(payload)
    assert caught.value.error.code == "ARIADNE_TASK_SCHEMA_MIGRATION_REQUIRED"

    store = SQLiteTaskStore(tmp_path / "legacy-tasks.sqlite3")
    with sqlite3.connect(store.path) as con:
        con.execute(
            """
            INSERT INTO tasks(
                task_id,session_id,status,revision,payload,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                state.task_id,
                state.session_id,
                state.status,
                1,
                json.dumps(payload, separators=(",", ":")),
                state.created_at,
                state.updated_at,
            ),
        )
    with pytest.raises(AriadneError) as stored:
        store.load(state.task_id)
    assert stored.value.error.code == "ARIADNE_TASK_SCHEMA_MIGRATION_REQUIRED"

    payload["schema_version"] = 99
    with pytest.raises(AriadneError) as unknown:
        TaskState.from_dict(payload)
    assert unknown.value.error.code == "ARIADNE_TASK_SCHEMA_UNSUPPORTED"


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


def test_verifier_rejects_unbounded_file_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.txt").write_text("x" * 11, encoding="utf-8")
    verifier = DeterministicVerifier(workspace, max_read_bytes=10)
    result = verifier.run(
        Check.from_plan(
            {
                "kind": "file_contains",
                "spec": {"path": "large.txt", "text": "x"},
            }
        ),
        traces=[],
        attempt_id="bounded-read",
    )
    assert result.status == "error"
    assert result.error is not None
    assert "max_read_bytes=10" in result.error.message


def test_resume_rechecks_current_step_against_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SQLiteTaskStore(tmp_path / "tasks.sqlite3")
    controller = TaskController(store=store, verifier=DeterministicVerifier(workspace))
    state = TaskState.from_plan(
        session_id="resume-session",
        user_id=None,
        original_user_goal="create marker",
        goal="create marker",
        steps=[
            {
                "intent": "create marker",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
                ],
            }
        ],
        goal_checks=[
            {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
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
        original_user_goal="edit an existing file",
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
        goal_checks=[
            {
                "kind": "file_contains",
                "spec": {"path": "/workspace/marker.txt", "text": "updated"},
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
                            "goal": "create a verified result",
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


def test_read_only_task_creates_attempt_and_completes(tmp_path: Path) -> None:
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
                            "goal": "inspect the existing report",
                            "steps": [
                                {
                                    "intent": "read the report",
                                    "done_when": [
                                        {
                                            "kind": "file_contains",
                                            "spec": {
                                                "path": "/workspace/report.txt",
                                                "text": "verified evidence",
                                            },
                                        }
                                    ],
                                }
                            ],
                            "goal_checks": [
                                {
                                    "kind": "file_contains",
                                    "spec": {
                                        "path": "/workspace/report.txt",
                                        "text": "verified evidence",
                                    },
                                }
                            ],
                        },
                        "plan-read",
                    )
                ],
            }
        if n == 1:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_read_file",
                        {"path": "/workspace/report.txt"},
                        "read-report",
                    )
                ],
            }
        assert model_tools is None
        return {"content": "The report was read and verified."}

    app, workspace, store = _app(tmp_path, script)
    (workspace / "report.txt").write_text("verified evidence\n", encoding="utf-8")
    result = asyncio.run(
        app.run(
            prompt="inspect the existing report",
            session_id="read-only-task",
            metadata={"task_mode": True},
        )
    )

    assert result.status == "completed"
    assert result.tool_calls[0].name == "sandbox_read_file"
    assert result.tool_calls[0].attempt_id.endswith(":1")
    assert store.load_active("read-only-task") is None


def test_task_plan_binds_original_goal_and_requires_goal_oracle(tmp_path: Path) -> None:
    controller = TaskController(
        store=SQLiteTaskStore(tmp_path / "tasks.sqlite3"),
        verifier=DeterministicVerifier(tmp_path),
    )
    base = {
        "steps": [
            {
                "intent": "inspect result",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "result.txt"}}
                ],
            }
        ],
        "goal_checks": [
            {"kind": "path_exists", "spec": {"path": "result.txt"}}
        ],
    }
    with pytest.raises(AriadneError) as weakened:
        controller.create_from_plan(
            session_id="goal-mismatch",
            user_id=None,
            original_user_goal="analyze every finding in the report",
            arguments={"goal": "check that result.txt exists", **base},
        )
    assert weakened.value.error.code == "ARIADNE_TASK_GOAL_MISMATCH"

    with pytest.raises(AriadneError) as missing_oracle:
        controller.create_from_plan(
            session_id="goal-check-missing",
            user_id=None,
            original_user_goal="inspect result",
            arguments={
                "goal": "inspect result",
                "steps": base["steps"],
            },
        )
    assert missing_oracle.value.error.code == "ARIADNE_TASK_INVALID"

    with pytest.raises(AriadneError) as optional_oracle:
        controller.create_from_plan(
            session_id="goal-check-optional",
            user_id=None,
            original_user_goal="inspect result",
            arguments={
                "goal": "inspect result",
                "steps": base["steps"],
                "goal_checks": [
                    {
                        "kind": "path_absent",
                        "spec": {"path": "result.txt"},
                        "required": False,
                    }
                ],
            },
        )
    assert optional_oracle.value.error.code == "ARIADNE_TASK_INVALID"

    with pytest.raises(AriadneError) as optional_step_check:
        controller.create_from_plan(
            session_id="step-check-optional",
            user_id=None,
            original_user_goal="inspect result",
            arguments={
                "goal": "inspect result",
                "steps": [
                    {
                        "intent": "inspect result",
                        "done_when": [
                            {
                                "kind": "path_exists",
                                "spec": {"path": "result.txt"},
                                "required": False,
                            }
                        ],
                    }
                ],
                "goal_checks": base["goal_checks"],
            },
        )
    assert optional_step_check.value.error.code == "ARIADNE_TASK_INVALID"


def test_skill_outcome_is_attributed_to_each_adopted_attempt(tmp_path: Path) -> None:
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
                            "goal": "use planner and finish both steps",
                            "steps": [
                                {
                                    "intent": "finish the first step",
                                    "done_when": [
                                        {
                                            "kind": "path_exists",
                                            "spec": {"path": "/workspace/first.txt"},
                                        }
                                    ],
                                },
                                {
                                    "intent": "finish the second step",
                                    "done_when": [
                                        {
                                            "kind": "file_contains",
                                            "spec": {
                                                "path": "/workspace/second.txt",
                                                "text": "expected",
                                            },
                                        }
                                    ],
                                    "failure_policy": "abort",
                                },
                            ],
                            "goal_checks": [
                                {
                                    "kind": "file_contains",
                                    "spec": {
                                        "path": "/workspace/second.txt",
                                        "text": "expected",
                                    },
                                }
                            ],
                        },
                        "plan-skill",
                    )
                ],
            }
        if n == 1:
            return {
                "content": "",
                "tool_calls": [
                    _tc("load_skill", {"name": "first-step-guide"}, "load-first-guide")
                ],
            }
        if n == 2:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "adopt_skill",
                        {
                            "name": "first-step-guide",
                            "reason": "follow its first-step checklist",
                        },
                        "adopt-first-guide",
                    )
                ],
            }
        if n == 3:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/first.txt", "content": "done\n"},
                        "write-first",
                    )
                ],
            }
        if n == 4:
            return {
                "content": "",
                "tool_calls": [
                    _tc("load_skill", {"name": "second-step-guide"}, "load-second-guide")
                ],
            }
        if n == 5:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "adopt_skill",
                        {
                            "name": "second-step-guide",
                            "reason": "follow its second-step checklist",
                        },
                        "adopt-second-guide",
                    )
                ],
            }
        if n == 6:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/second.txt", "content": "wrong\n"},
                        "write-second",
                    )
                ],
            }
        assert model_tools is None
        return {"content": "The second step failed verification."}

    app, _, store = _app(tmp_path, script)
    ledger = SkillOutcomeLedger(tmp_path / "skill-outcomes.json", min_samples=1)
    app.skills.outcome_ledger = ledger
    app.skills.manage(
        action="create",
        name="first-step-guide",
        description="finish the first step with verification",
        body="Create and verify the first-step marker.",
        keywords=["first", "steps"],
    )
    app.skills.manage(
        action="create",
        name="second-step-guide",
        description="finish the second step with verification",
        body="Create and verify the expected second-step content.",
        keywords=["second", "steps"],
    )

    result = asyncio.run(
        app.run(
            prompt="use planner and finish both steps",
            session_id="skill-attribution",
            metadata={"task_mode": True},
        )
    )
    assert result.status == "failed"
    state = store.list_for_session("skill-attribution")[0]
    assert [item.status for item in state.steps] == ["verified", "failed"]
    first_event = ledger.list_events(skill_name="first-step-guide")[-1]
    assert first_event["step_outcome"] == "verified"
    assert first_event["task_outcome"] == "active"
    assert first_event["step_id"] == state.steps[0].step_id
    assert first_event["attempt_id"].startswith(f"{state.steps[0].step_id}:")
    assert first_event["tool_names_used"] == ["sandbox_write_file"]
    assert ledger.adjustment("first-step-guide").positive == 1

    second_event = ledger.list_events(skill_name="second-step-guide")[-1]
    assert second_event["step_outcome"] == "failed"
    assert second_event["task_outcome"] == "failed"
    assert second_event["step_id"] == state.steps[1].step_id
    assert second_event["attempt_id"].startswith(f"{state.steps[1].step_id}:")
    assert second_event["tool_names_used"] == ["sandbox_write_file"]
    assert ledger.adjustment("second-step-guide").negative == 1


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
                            "goal_checks": [
                                {
                                    "kind": "file_contains",
                                    "spec": {
                                        "path": "/workspace/value.txt",
                                        "text": "good",
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
        original_user_goal="write marker",
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
        goal_checks=[
            {"kind": "path_exists", "spec": {"path": "/workspace/marker.txt"}}
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
        original_user_goal="write safe marker",
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
        goal_checks=[
            {
                "kind": "path_exists",
                "spec": {"path": "/workspace/safe-marker.txt"},
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
                            "goal": "make a marker",
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
                            "goal_checks": [
                                {
                                    "kind": "path_exists",
                                    "spec": {"path": "/workspace/marker.txt"},
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
