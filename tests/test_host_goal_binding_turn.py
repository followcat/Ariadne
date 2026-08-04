"""Kernel-path Host task→goal binding (TurnApplication, not capture helpers).

Covers the production plan-submit wiring: reuse an earlier auto-captured goal
instead of always inventing goal:<plan_turn_id>.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tasks import DeterministicVerifier, SQLiteTaskStore, TaskController
from ariadne.tasks.controller import SUBMIT_TASK_PLAN_NAME
from ariadne.tools.registry import build_default_registry


def _tc(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _run_turn(app: TurnApplication, *, prompt: str, session_id: str) -> Any:
    async def collect() -> Any:
        result = None
        async for ev in app.run_events(
            prompt=prompt,
            session_id=session_id,
            metadata={"task_mode": True},
        ):
            if ev.kind in {"turn_completed", "turn_failed"}:
                result = ev.data.get("result")
        return result

    return asyncio.run(collect())


def test_plan_submit_reuses_prior_auto_captured_goal(tmp_path: Path) -> None:
    """Phrase on t1, plan submit on t2 → bind goal:t1, do not orphan as goal:t2."""

    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = tmp_path / "data"
    memory = Memory.local(path=data / "memory")
    skills = SkillStore.from_dirs([], strict=False, user_root=data / "skills")
    tools = build_default_registry(
        memory=memory, skills=skills, enable_deferred_demo=False
    )
    controller = TaskController(
        store=SQLiteTaskStore(data / "tasks.sqlite3"),
        verifier=DeterministicVerifier(workspace),
    )

    # Turn 1: free chat materializes a lifecycle goal from the user phrase.
    # (task_mode on but model answers without tools → may needs_input; we only
    # need capture of the goal phrase. Use task_mode off for phrase capture.)
    app_phrase = TurnApplication(
        model=FakeModel(script=lambda m, t: {"content": "收到，先记住目标。"}),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=data),
        task_controller=controller,
        guardrails_enabled=False,
        tool_loop_limit=4,
        task_mode_policy="off",
    )

    async def phrase_turn() -> Any:
        result = None
        async for ev in app_phrase.run_events(
            prompt="目标是修复认证超时。",
            session_id="bind-reuse",
        ):
            if ev.kind in {"turn_completed", "turn_failed"}:
                result = ev.data.get("result")
        return result

    phrase_result = asyncio.run(phrase_turn())
    assert phrase_result is not None
    prior = memory.state.current_goal_id("bind-reuse")
    assert prior == "goal:t1" or (
        prior is not None and prior.startswith("goal:")
    )
    # Capture may use a different turn_id hex than "t1"; keep the actual id.
    prior_goal_id = prior
    assert prior_goal_id is not None

    # Plan goal must equal this turn's user prompt. Avoid re-matching the
    # deterministic "目标是…" extractor so capture does not open a second goal.
    plan_prompt = "修复认证超时"
    plan_args = {
        "goal": plan_prompt,
        "goal_checks": [
            {"kind": "path_exists", "spec": {"path": "/workspace/auth_ok.txt"}},
        ],
        "steps": [
            {
                "intent": "write auth ok marker",
                "done_when": [
                    {
                        "kind": "path_exists",
                        "spec": {"path": "/workspace/auth_ok.txt"},
                    }
                ],
            }
        ],
    }
    phase = {"n": 0}

    def script(messages: list[dict[str, Any]], tools_payload: Any) -> dict[str, Any]:
        phase["n"] += 1
        if phase["n"] == 1:
            return {
                "content": "",
                "tool_calls": [_tc(SUBMIT_TASK_PLAN_NAME, plan_args, "plan1")],
            }
        if phase["n"] == 2:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {
                            "path": "/workspace/auth_ok.txt",
                            "content": "ok\n",
                        },
                        "w1",
                    )
                ],
            }
        return {"content": "认证修复完成。"}

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
    result = _run_turn(app, prompt=plan_prompt, session_id="bind-reuse")
    assert result is not None
    assert result.status == "completed"
    assert result.task is not None
    assert result.task.status == "completed"

    bound = memory.state.goal_id_for_task("bind-reuse", result.task.task_id)
    assert bound == prior_goal_id
    # Plan-turn must not leave a second active orphan goal as current.
    assert memory.state.current_goal_id("bind-reuse") == prior_goal_id
    goal = memory.state.get("bind-reuse")["entities"][prior_goal_id]
    assert goal["status"] == "done"
    assert goal["status_authority"] == "verified_check"
    # No extra lifecycle goal forced solely by plan-turn id (unless it equals prior).
    plan_only = f"goal:{result.turn_id}"
    if plan_only != prior_goal_id:
        plan_entity = (memory.state.get("bind-reuse").get("entities") or {}).get(
            plan_only
        )
        assert plan_entity is None or plan_entity.get("status") != "active"
    assert prior_goal_id in (memory.state.get("bind-reuse").get("entities") or {})


def test_plan_submit_materializes_goal_when_none_exists(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = tmp_path / "data"
    memory = Memory.local(path=data / "memory")
    skills = SkillStore.from_dirs([], strict=False, user_root=data / "skills")
    tools = build_default_registry(
        memory=memory, skills=skills, enable_deferred_demo=False
    )
    controller = TaskController(
        store=SQLiteTaskStore(data / "tasks.sqlite3"),
        verifier=DeterministicVerifier(workspace),
    )
    plan_prompt = "写文件"
    plan_args = {
        "goal": plan_prompt,
        "goal_checks": [
            {"kind": "path_exists", "spec": {"path": "/workspace/m.txt"}},
        ],
        "steps": [
            {
                "intent": "write",
                "done_when": [
                    {"kind": "path_exists", "spec": {"path": "/workspace/m.txt"}}
                ],
            }
        ],
    }
    phase = {"n": 0}

    def script(messages: list[dict[str, Any]], tools_payload: Any) -> dict[str, Any]:
        phase["n"] += 1
        if phase["n"] == 1:
            return {
                "content": "",
                "tool_calls": [_tc(SUBMIT_TASK_PLAN_NAME, plan_args, "p1")],
            }
        if phase["n"] == 2:
            return {
                "content": "",
                "tool_calls": [
                    _tc(
                        "sandbox_write_file",
                        {"path": "/workspace/m.txt", "content": "x\n"},
                        "w1",
                    )
                ],
            }
        return {"content": "done"}

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
    result = _run_turn(app, prompt=plan_prompt, session_id="bind-mint")
    assert result is not None and result.status == "completed"
    assert result.task is not None
    bound = memory.state.goal_id_for_task("bind-mint", result.task.task_id)
    assert bound == f"goal:{result.turn_id}"
    goal = memory.state.get("bind-mint")["entities"][bound]
    assert goal["status"] == "done"
