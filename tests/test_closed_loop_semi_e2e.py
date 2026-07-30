"""Semi-integration: multi-exchange FakeModel closed-loop (plan → write → verify).

Exercises the real TurnApplication path after runtime extraction — not a unit of
a single helper. Still offline (no live LLM).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.kernel.turn import TurnApplication
from ariadne.memory.facade import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tasks import DeterministicVerifier, SQLiteTaskStore, TaskController
from ariadne.tasks.controller import SUBMIT_TASK_PLAN_NAME
from ariadne.tasks.policy import resolve_task_mode
from ariadne.tools.registry import build_default_registry
from ariadne.types import TurnEvent


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_semi_e2e_plan_write_verify_complete(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = tmp_path / "data"
    marker = "hello-closed-loop"

    user_goal = "create marker.txt with greeting"
    plan_args = {
        "goal": user_goal,
        "goal_checks": [
            {
                "kind": "path_exists",
                "spec": {"path": "/workspace/marker.txt"},
            },
            {
                "kind": "file_contains",
                "spec": {"path": "/workspace/marker.txt", "text": marker},
            },
        ],
        "steps": [
            {
                "intent": "write marker file",
                "done_when": [
                    {
                        "kind": "path_exists",
                        "spec": {"path": "/workspace/marker.txt"},
                    },
                    {
                        "kind": "file_contains",
                        "spec": {"path": "/workspace/marker.txt", "text": marker},
                    },
                ],
            }
        ],
    }

    phase = {"n": 0}

    def script(messages: list[dict[str, Any]], tools: Any) -> dict[str, Any]:
        phase["n"] += 1
        if phase["n"] == 1:
            return {
                "content": "",
                "tool_calls": [
                    _tc(SUBMIT_TASK_PLAN_NAME, plan_args, "plan1"),
                ],
            }
        if phase["n"] == 2:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/marker.txt", "content": marker + "\n"},
                        "w1",
                    )
                ],
            }
        return {"content": "marker ready"}

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
        tool_loop_limit=12,
        task_mode_policy="auto",
    )

    kinds: list[str] = []

    async def collect() -> Any:
        result = None
        async for ev in app.run_events(
            prompt=user_goal,
            session_id="semi-e2e",
            metadata={"task_mode": True},
        ):
            kinds.append(ev.kind)
            if ev.kind in {"turn_completed", "turn_failed"}:
                result = ev.data.get("result")
        return result

    result = asyncio.run(collect())
    assert result is not None
    assert result.status == "completed"
    assert (workspace / "marker.txt").read_text(encoding="utf-8").startswith(marker)
    assert "task_mode_resolved" in kinds
    assert "task_started" in kinds
    assert "task_step_started" in kinds
    assert "task_check_completed" in kinds
    assert "task_completed" in kinds
    assert result.task is not None
    assert result.task.status == "completed"


def test_auto_policy_resumes_without_metadata_flag(tmp_path: Path) -> None:
    """After a task exists, policy=auto enables task mode without metadata."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = tmp_path / "data"
    store = SQLiteTaskStore(data / "tasks.sqlite3")
    controller = TaskController(
        store=store,
        verifier=DeterministicVerifier(workspace),
    )
    state = controller.create_from_plan(
        session_id="s-resume",
        user_id="local",
        original_user_goal="hold",
        arguments={
            "goal": "hold",
            "steps": [
                {
                    "intent": "wait",
                    "done_when": [
                        {"kind": "path_exists", "spec": {"path": "/workspace/never"}}
                    ],
                }
            ],
            "goal_checks": [
                {"kind": "path_exists", "spec": {"path": "/workspace/never"}}
            ],
        },
    )
    assert store.load_active("s-resume") is not None
    enabled, reason = resolve_task_mode(
        policy="auto",
        metadata=None,
        has_active_task=store.load_active("s-resume") is not None,
    )
    assert enabled is True
    assert reason == "active_task_resume"
    assert state.task_id
