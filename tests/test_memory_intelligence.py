from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import EvidenceRef, Memory
from ariadne.memory.auto_capture import (
    AutomaticMemoryProjector,
    make_llm_memory_extractor,
)
from ariadne.memory.capture_journal import CaptureJournalStore
from ariadne.memory.reflection import ReflectionStore
from ariadne.memory.state import ConversationStateStore
from ariadne.model.fake import FakeModel
from ariadne.redact import redact_secrets, redact_text
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolContext, ToolSpec, build_default_registry


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _capture(
    memory: Memory,
    *,
    session_id: str,
    turn_id: str,
    user: str,
    assistant: str = "好的。",
    tool_calls: list[Any] | None = None,
    verified_goal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if verified_goal and verified_goal.get("task_id"):
        goal_id = memory.state.current_goal_id(session_id)
        if goal_id is not None:
            memory.state.bind_task_goal(
                session_id=session_id,
                task_id=str(verified_goal["task_id"]),
                goal_id=goal_id,
                source_turn_id=turn_id,
                evidence_text=user,
                idempotency_key=f"test:{session_id}:{verified_goal['task_id']}",
            )
    return _run(
        memory.capture_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_text=user,
            assistant_text=assistant,
            tool_calls=tool_calls or [],
            verified_goal=verified_goal,
        )
    )


def _current_goal(memory: Memory, session_id: str) -> tuple[str, dict[str, Any]]:
    state = memory.state.get(session_id)
    goal_id = memory.state.current_goal_id_from_state(state)
    assert goal_id is not None
    return goal_id, state["entities"][goal_id]


def _event(
    event_type: str,
    content: str,
    *,
    session_id: str,
    turn_id: str,
    entities: list[str] | None = None,
    reason: str = "",
    relation: dict[str, str] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "type": event_type,
        "content": content,
        "reason": reason,
        "entities": entities or [],
        "metadata": {},
        "evidence": [
            EvidenceRef(
                session_id=session_id,
                turn_id=turn_id,
                source="user",
                quote=content,
            ).to_dict()
        ],
    }
    if relation is not None:
        row["relation"] = relation
    return row


def test_explicit_preference_auto_capture_supersedes_one_temporal_key(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")

    _capture(
        memory,
        session_id="s1",
        turn_id="t1",
        user="以后 Python 项目都用 poetry，不用 pip 了",
    )
    _capture(
        memory,
        session_id="s2",
        turn_id="t2",
        user="以后 Python 项目都用 uv，不用 poetry 了",
    )

    assert memory.user_model is not None
    rows = [
        row
        for row in memory.user_model.list()
        if row["key"] == "python_package_manager"
    ]
    assert len(rows) == 1
    current = rows[0]
    assert current["value"] == "uv"
    assert current["previous_value"] == "poetry"
    assert current["valid_until"] is None
    assert current["history"][-1]["value"] == "poetry"
    assert current["history"][-1]["status"] == "superseded"
    assert current["history"][-1]["valid_until"] <= current["valid_from"]
    assert current["evidence"][0]["turn_id"] == "t2"
    timeline = memory.user_model.timeline(
        entry_type="preference", key="python_package_manager"
    )
    assert [row["value"] for row in timeline] == ["poetry", "uv"]
    old_midpoint = (
        float(timeline[0]["valid_from"]) + float(timeline[0]["valid_until"])
    ) / 2
    as_of = memory.user_model.get_as_of(timestamp=old_midpoint)
    assert next(row for row in as_of if row["key"] == "python_package_manager")[
        "value"
    ] == "poetry"


def test_temporary_discussion_does_not_create_durable_preference(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")

    report = _capture(
        memory,
        session_id="s1",
        turn_id="t1",
        user="我们讨论一下 uv 和 poetry 哪个更适合这个临时 demo。",
    )

    assert report["user_model_entry_ids"] == []
    assert memory.user_model is not None
    assert memory.user_model.list() == []


def test_ambiguous_capture_uses_llm_but_simple_explicit_change_does_not(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    calls: list[dict[str, Any]] = []

    async def extractor(payload: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(payload)
        return [
            {
                "type": "observation",
                "content": "user referred to the previous setting",
                "evidence_quote": "之前那个设置",
            }
        ]

    assert memory.auto_capture is not None
    memory.auto_capture.extractor = extractor
    simple = _capture(
        memory,
        session_id="s1",
        turn_id="t1",
        user="以后 Python 项目都用 uv，不用 poetry 了",
    )
    assert simple["llm_used"] is False
    assert calls == []

    ambiguous = _capture(
        memory,
        session_id="s1",
        turn_id="t2",
        user="还是按之前那个设置来。",
    )
    assert ambiguous["llm_used"] is True
    assert len(calls) == 1
    episode = memory.episodes.for_turn(session_id="s1", turn_id="t2")
    assert episode is not None
    assert any(
        event["content"] == "user referred to the previous setting"
        for event in episode["events"]
    )


def test_consecutive_turns_form_episode_with_decision_reason_and_outcome(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")

    _capture(
        memory,
        session_id="login",
        turn_id="t1",
        user="目标是修复登录超时。",
    )
    _capture(
        memory,
        session_id="login",
        turn_id="t2",
        user="尝试修改 timeout，结果没有变化。",
    )
    _capture(
        memory,
        session_id="login",
        turn_id="t3",
        user="决定更换 DNS，因为查询偶发需要 5 秒。",
    )
    _capture(
        memory,
        session_id="login",
        turn_id="t4",
        user="更换 DNS 后测试通过，问题已解决。",
        verified_goal={
            "status": "completed",
            "task_id": "task-login",
            "goal": "修复登录超时",
            "summary": "登录超时回归检查已通过",
            "check_ids": ["login-regression"],
        },
    )

    episodes = memory.episodes.list(session_id="login")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["related_turn_ids"] == ["t1", "t2", "t3", "t4"]
    event_types = {event["type"] for event in episode["events"]}
    assert {"goal", "attempt", "observation", "decision", "outcome"} <= event_types
    decision = next(event for event in episode["events"] if event["type"] == "decision")
    assert "查询偶发需要 5 秒" in decision["reason"]
    assert episode["status"] == "completed"
    goal_id, current_goal = _current_goal(memory, "login")
    assert goal_id == "goal:t1"
    assert current_goal["attributes"]["description"]["value"] == "修复登录超时"
    assert current_goal["status"] == "done"
    assert current_goal["status_authority"] == "verified_check"


def test_why_query_returns_decision_episode_with_real_citations(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="architecture",
        turn_id="t-redis",
        user="目标是选择缓存方案。决定不采用 Redis，因为单机部署下 SQLite 已满足并发要求。",
    )

    result = _run(
        memory.memory_search(
            query="为什么没有采用 Redis？",
            session_id="architecture",
            scope="session",
            mode="deep",
        )
    )

    assert result["hits"]
    hit = result["hits"][0]
    assert hit["evidence"]["source"] == "episode"
    assert any(event["type"] == "decision" for event in hit["event_chain"])
    assert hit["citations"]
    assert {citation["turn_id"] for citation in hit["citations"]} == {"t-redis"}
    assert "locate_decision" in hit["traversal_steps"]


def test_l2_as_of_replays_relations_status_and_collections(tmp_path: Path) -> None:
    store = ConversationStateStore(tmp_path / "state.json")
    evidence = "alpha beta todos done"
    store.apply_ops(
        session_id="s1",
        source_turn_id="t1",
        evidence_text=evidence,
        operations=[
            {"op": "ensure_entity", "entity_id": "alpha", "evidence_quote": "alpha"},
            {"op": "ensure_entity", "entity_id": "beta", "evidence_quote": "beta"},
            {
                "op": "set_relation",
                "relation": "depends_on",
                "from": "alpha",
                "to": "beta",
                "evidence_quote": "alpha",
            },
            {"op": "set_status", "entity_id": "alpha", "status": "active", "evidence_quote": "alpha"},
            {"op": "ensure_collection", "name": "todos", "evidence_quote": "todos"},
            {"op": "collection_append", "name": "todos", "member": "alpha", "evidence_quote": "alpha"},
        ],
    )
    store.apply_ops(
        session_id="s1",
        source_turn_id="t2",
        evidence_text=evidence,
        operations=[
            {
                "op": "remove_relation",
                "relation": "depends_on",
                "from": "alpha",
                "to": "beta",
                "evidence_quote": "alpha",
            },
            {"op": "set_status", "entity_id": "alpha", "status": "done", "evidence_quote": "done"},
            {"op": "collection_remove", "name": "todos", "member": "alpha", "evidence_quote": "alpha"},
        ],
    )

    old = store.get_as_of("s1", allowed_turn_ids={"t1"})
    assert old["entities"]["alpha"]["status"] == "active"
    assert old["relations"]["depends_on"] == [{"from": "alpha", "to": "beta"}]
    assert old["collections"]["todos"]["members"] == ["alpha"]
    current = store.get_as_of("s1", allowed_turn_ids={"t1", "t2"})
    assert current["entities"]["alpha"]["status"] == "done"
    assert current["relations"]["depends_on"] == []
    assert current["collections"]["todos"]["members"] == []


def test_multi_hop_traverses_relation_timeline_and_latest_outcome(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    sid = "payments"
    memory.episodes.append_turn(
        session_id=sid,
        turn_id="t1",
        workspace_key="/Ariadne",
        events=[
            _event(
                "problem",
                "支付项目出现退款问题",
                session_id=sid,
                turn_id="t1",
                entities=["支付项目", "退款问题"],
            )
        ],
        close_episode=True,
    )
    memory.episodes.append_turn(
        session_id=sid,
        turn_id="t2",
        workspace_key="/Ariadne",
        events=[
            _event(
                "entity_change",
                "退款问题转给小王",
                session_id=sid,
                turn_id="t2",
                entities=["退款问题", "小王"],
                relation={"type": "assigned_to", "from": "退款问题", "to": "小王"},
            )
        ],
        close_episode=True,
    )
    memory.episodes.append_turn(
        session_id=sid,
        turn_id="t3",
        workspace_key="/Ariadne",
        events=[
            _event(
                "outcome",
                "小王修复了退款问题，回归测试通过",
                session_id=sid,
                turn_id="t3",
                entities=["退款问题", "小王"],
            )
        ],
        close_episode=True,
    )

    result = _run(
        memory.memory_search(
            query="上次支付项目转给小王的问题最后解决了吗？",
            session_id=sid,
            scope="session",
            mode="deep",
        )
    )

    assert result["mode_used"] == "deep"
    assert result["hits"]
    hit = next(
        row
        for row in result["hits"]
        if any(event["type"] == "outcome" for event in row.get("event_chain") or [])
    )
    assert {"follow_relation", "retrieve_timeline", "locate_outcome"} <= set(
        hit["traversal_steps"]
    )
    assert any(event["type"] == "outcome" for event in hit["event_chain"])
    assert hit["related_turn_ids"] == ["t1", "t2", "t3"]


def test_reflection_requires_three_sessions_and_user_acceptance(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    for index in range(1, 4):
        _capture(
            memory,
            session_id=f"review-{index}",
            turn_id=f"t{index}",
            user="代码 review 时先看测试覆盖。",
        )

    assert memory.reflection is not None
    pending = memory.reflection.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["key"] == "review_order"
    assert pending[0]["session_count"] == 3
    assert memory.user_model is not None
    assert not any(row["key"] == "review_order" for row in memory.user_model.list())

    accepted = memory.reflection.decide(
        candidate_id=pending[0]["candidate_id"],
        action="accept",
        confirmation_token=memory.reflection.confirmation_token(
            candidate_id=pending[0]["candidate_id"],
            action="accept",
            session_id="review-confirm",
        ),
        confirmation_session_id="review-confirm",
        confirmation_turn_id="confirm-turn",
        user_model=memory.user_model,
        session_id="review-confirm",
    )
    assert accepted["status"] == "accepted"
    review = next(row for row in memory.user_model.list() if row["key"] == "review_order")
    assert review["value"] == "tests_first"
    assert review["source"] == "model_inferred"


def test_reflection_tool_cannot_self_confirm_without_current_user_consent(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    for index in range(1, 4):
        _capture(
            memory,
            session_id=f"review-{index}",
            turn_id=f"t{index}",
            user="代码 review 时先看测试覆盖。",
        )
    candidate_id = memory.reflection.list(status="pending")[0]["candidate_id"]
    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    denied_ctx = ToolContext(
        session_id="s4",
        turn_id="t4",
        sandbox=None,
        memory=memory,
        user_text="有哪些建议？",
    )
    with pytest.raises(AriadneError) as caught:
        _run(
            registry.invoke(
                "memory_reflection",
                {"action": "accept", "candidate_id": candidate_id},
                denied_ctx,
            )
        )
    assert caught.value.error.code == "ARIADNE_TOOL_DENIED"

    list_ctx = ToolContext(
        session_id="s4",
        turn_id="t5",
        sandbox=None,
        memory=memory,
        user_text="列出待确认建议",
    )
    listed = _run(
        registry.invoke(
            "memory_reflection",
            {"action": "list", "status": "pending"},
            list_ctx,
        )
    )
    contract = listed["candidates"][0]["confirmation_contracts"]["accept"]
    token = contract.rsplit(" ", 1)[-1]
    reject_contract = listed["candidates"][0]["confirmation_contracts"]["reject"]
    reject_token = reject_contract.rsplit(" ", 1)[-1]

    negative_ctx = ToolContext(
        session_id="s4",
        turn_id="t6",
        sandbox=None,
        memory=memory,
        user_text="我不同意接受这个建议，请拒绝它。",
    )
    with pytest.raises(AriadneError) as negative:
        _run(
            registry.invoke(
                "memory_reflection",
                {
                    "action": "accept",
                    "candidate_id": candidate_id,
                    "confirmation_token": token,
                },
                negative_ctx,
            )
        )
    assert negative.value.error.code == "ARIADNE_TOOL_DENIED"

    wrong_action_ctx = ToolContext(
        session_id="s4",
        turn_id="t6-wrong-action",
        sandbox=None,
        memory=memory,
        user_text=reject_contract,
    )
    with pytest.raises(AriadneError) as wrong_action:
        _run(
            registry.invoke(
                "memory_reflection",
                {
                    "action": "accept",
                    "candidate_id": candidate_id,
                    "confirmation_token": reject_token,
                },
                wrong_action_ctx,
            )
        )
    assert wrong_action.value.error.code == "ARIADNE_TOOL_DENIED"

    accepted_ctx = ToolContext(
        session_id="s4",
        turn_id="t7",
        sandbox=None,
        memory=memory,
        user_text=contract,
    )
    accepted = _run(
        registry.invoke(
            "memory_reflection",
            {
                "action": "accept",
                "candidate_id": candidate_id,
                "confirmation_token": token,
            },
            accepted_ctx,
        )
    )
    assert accepted["status"] == "accepted"


def test_explicit_prospective_memory_matches_once_and_enters_context(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    report = _capture(
        memory,
        session_id="s1",
        turn_id="t1",
        user="下次修改 auth/* 时提醒检查 token migration。",
    )

    assert memory.prospective is not None
    assert len(report["prospective_entry_ids"]) == 1
    entry_id = report["prospective_entry_ids"][0]
    assert memory.prospective.list()[0]["status"] == "pending"
    first = memory.prospective.match(
        context={
            "workspace": "",
            "text": "edit auth",
            "changed_paths": ["auth/login.py"],
            "tool_names": [],
            "event_types": [],
            "entity_ids": [],
        }
    )
    second = memory.prospective.match(
        context={
            "workspace": "",
            "text": "edit auth",
            "changed_paths": ["auth/login.py"],
            "tool_names": [],
            "event_types": [],
            "entity_ids": [],
        }
    )
    assert [row["entry_id"] for row in first] == [entry_id]
    assert second == []
    text, summary = memory.build_context(session_id="s2", query="继续工作")
    assert "检查 token migration" in text
    layer = next(row for row in summary.layers if row.name == "prospective")
    assert layer.status == "used"


def test_turn_completion_auto_captures_without_memory_tool_call(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = tmp_path / "data"
    skills = SkillStore({})
    tools = build_default_registry(memory=memory, skills=skills)
    app = TurnApplication(
        model=FakeModel(script=lambda messages, tool_payload: {"content": "明白。"}),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=data),
        task_mode_policy="off",
    )

    result = _run(
        app.run(
            prompt="以后 Python 项目都用 uv，不用 poetry 了",
            session_id="s1",
        )
    )

    assert result.status == "completed"
    assert all(call.name != "memory" for call in result.tool_calls)
    capture_layer = next(row for row in result.memory.layers if row.name == "auto_capture")
    assert capture_layer.status == "used"
    assert memory.user_model is not None
    assert next(
        row for row in memory.user_model.list() if row["key"] == "python_package_manager"
    )["value"] == "uv"


def test_episode_search_keeps_grounded_turn_and_session_contract(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="s1",
        turn_id="real-turn",
        user="决定采用 SQLite，因为这是个人单机部署。",
    )

    result = _run(
        memory.memory_search(
            query="个人单机 SQLite 决定",
            session_id="s1",
            scope="session",
            mode="fast",
        )
    )

    assert result["hits"]
    for hit in result["hits"]:
        assert hit["turn_id"]
        assert hit["session_id"]
        assert hit["evidence"]["source"] in {
            "raw",
            "summary",
            "chunk",
            "curated",
            "episode",
        }


def test_assistant_assertion_cannot_complete_authoritative_goal(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="goal",
        turn_id="t1",
        user="目标是修复认证问题。",
    )
    _capture(
        memory,
        session_id="goal",
        turn_id="t2",
        user="继续",
        assistant="测试通过，修复完成。",
    )

    _goal_id, goal = _current_goal(memory, "goal")
    assert goal["status"] == "active"
    assert goal["status_authority"] == "user_explicit"
    episode = memory.episodes.for_turn(session_id="goal", turn_id="t2")
    assert episode is not None
    assertion = next(
        event
        for event in episode["events"]
        if event["turn_id"] == "t2" and event["type"] == "outcome"
    )
    assert assertion["metadata"]["authority"] == "model_assertion"
    assert assertion["metadata"]["terminal"] is False
    assert episode["status"] == "active"


def test_verified_goal_checks_can_complete_authoritative_goal(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="verified-goal",
        turn_id="t1",
        user="目标是修复认证问题。",
    )
    _capture(
        memory,
        session_id="verified-goal",
        turn_id="t2",
        user="继续",
        assistant="修复完成。",
        verified_goal={
            "status": "completed",
            "task_id": "task-1",
            "goal": "修复认证问题",
            "summary": "认证回归检查已通过",
            "check_ids": ["check-1"],
        },
    )

    _goal_id, goal = _current_goal(memory, "verified-goal")
    assert goal["status"] == "done"
    assert goal["status_authority"] == "verified_check"


def test_completed_goal_a_can_be_followed_by_distinct_active_goal_b(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="goal-sequence",
        turn_id="t1",
        user="目标是完成任务 A。",
    )
    _capture(
        memory,
        session_id="goal-sequence",
        turn_id="t2",
        user="继续",
        verified_goal={
            "status": "completed",
            "task_id": "task-a",
            "goal": "完成任务 A",
            "summary": "任务 A 已验证完成",
            "check_ids": ["check-a"],
        },
    )
    report_b = _capture(
        memory,
        session_id="goal-sequence",
        turn_id="t3",
        user="新目标是完成任务 B。",
    )

    assert report_b["status"] == "used"
    journal_b = memory.capture_journal.get(
        workspace_key="",
        session_id="goal-sequence",
        turn_id="t3",
    )
    assert journal_b is not None and journal_b["status"] == "completed"
    state = memory.state.get("goal-sequence")
    pointer = state["entities"]["session:current_goal"]
    assert pointer["type"] == "goal_pointer"
    goal_b_id = pointer["attributes"]["goal_id"]["value"]
    assert goal_b_id == "goal:t3"
    goal_a = state["entities"]["goal:t1"]
    goal_b = state["entities"][goal_b_id]
    assert goal_a["status"] == "done"
    assert goal_a["status_authority"] == "verified_check"
    assert goal_b["status"] == "active"
    assert goal_b["attributes"]["description"]["value"] == "完成任务 B"
    episode_b = memory.episodes.for_turn(
        session_id="goal-sequence",
        turn_id="t3",
    )
    assert episode_b is not None and episode_b["status"] == "active"


def test_same_turn_verified_a_and_proposed_b_binds_terminal_to_a(
    tmp_path: Path,
) -> None:
    """A active → same turn A verified + B proposed → A done, B active."""

    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="same-turn-goals",
        turn_id="t1",
        user="目标是完成任务 A。",
    )
    report = _capture(
        memory,
        session_id="same-turn-goals",
        turn_id="t2",
        user="新目标是完成任务 B。",
        assistant="任务 A 的检查已通过，开始任务 B。",
        verified_goal={
            "status": "completed",
            "task_id": "task-a",
            "goal": "完成任务 A",
            "summary": "任务 A 已验证完成",
            "check_ids": ["check-a"],
        },
    )
    assert report["status"] == "used"
    state = memory.state.get("same-turn-goals")
    goal_a = state["entities"]["goal:t1"]
    goal_b = state["entities"]["goal:t2"]
    pointer = state["entities"]["session:current_goal"]
    assert goal_a["status"] == "done"
    assert goal_a["status_authority"] == "verified_check"
    assert goal_a["attributes"]["task_id"]["value"] == "task-a"
    assert goal_b["status"] == "active"
    assert pointer["attributes"]["goal_id"]["value"] == "goal:t2"
    assert memory.state.current_goal_id("same-turn-goals") == "goal:t2"
    assert memory.state.goal_id_for_task("same-turn-goals", "task-a") == "goal:t1"

    closed = memory.episodes.for_turn_segment(
        session_id="same-turn-goals",
        turn_id="t2",
        segment="close",
    )
    opened = memory.episodes.for_turn_segment(
        session_id="same-turn-goals",
        turn_id="t2",
        segment="open",
    )
    assert closed is not None and closed["status"] == "completed"
    assert opened is not None and opened["status"] == "active"
    assert any(event.get("type") == "outcome" for event in closed["events"])
    assert any(
        event.get("type") == "goal" and "任务 B" in str(event.get("content") or "")
        for event in opened["events"]
    )
    primary = memory.episodes.for_turn(session_id="same-turn-goals", turn_id="t2")
    assert primary is not None and primary["episode_id"] == opened["episode_id"]


def test_new_goal_migrates_legacy_fixed_goal_without_reactivation(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    memory.state.apply_ops(
        session_id="legacy-goal",
        source_turn_id="legacy-turn",
        evidence_text="旧目标已完成",
        operations=[
            {
                "op": "ensure_entity",
                "entity_id": "session:current_goal",
                "type": "goal",
                "evidence_quote": "旧目标",
            },
            {
                "op": "set_attribute",
                "entity_id": "session:current_goal",
                "key": "description",
                "value": "旧目标",
                "memory_type": "goal",
                "authority": "user_explicit",
                "evidence_quote": "旧目标",
            },
            {
                "op": "set_status",
                "entity_id": "session:current_goal",
                "status": "done",
                "authority": "verified_check",
                "evidence_quote": "已完成",
            },
        ],
    )

    _capture(
        memory,
        session_id="legacy-goal",
        turn_id="new-turn",
        user="新目标是完成迁移后的任务。",
    )

    state = memory.state.get("legacy-goal")
    pointer = state["entities"]["session:current_goal"]
    assert pointer["type"] == "goal_pointer"
    assert pointer["attributes"]["goal_id"]["value"] == "goal:new-turn"
    legacy = [
        entity
        for entity_id, entity in state["entities"].items()
        if entity_id.startswith("goal:legacy:")
    ]
    assert len(legacy) == 1
    assert legacy[0]["status"] == "done"
    assert legacy[0]["status_authority"] == "verified_check"
    assert legacy[0]["attributes"]["description"]["value"] == "旧目标"
    assert state["entities"]["goal:new-turn"]["status"] == "active"


@pytest.mark.parametrize(
    "statement",
    [
        "测试通过了吗？",
        "还没有修复完成。",
        "修复完成前不要关闭任务。",
        "不要取消这个任务，继续处理。",
        "不要放弃这个任务。",
        "问题已解决。",
    ],
)
def test_free_text_terminal_language_never_closes_authoritative_goal(
    tmp_path: Path,
    statement: str,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="free-text-goal",
        turn_id="t1",
        user="目标是修复认证问题。",
    )
    _capture(
        memory,
        session_id="free-text-goal",
        turn_id="t2",
        user=statement,
    )

    _goal_id, goal = _current_goal(memory, "free-text-goal")
    assert goal["status"] == "active"
    episode = memory.episodes.for_turn(
        session_id="free-text-goal",
        turn_id="t2",
    )
    assert episode is not None and episode["status"] == "active"


def test_model_facing_state_tool_cannot_supply_evidence_authority_or_terminal_status(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="state-authority",
        turn_id="t1",
        user="目标是完成安全检查。",
    )
    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    ctx = ToolContext(
        session_id="state-authority",
        turn_id="t2",
        sandbox=None,
        memory=memory,
        user_text="继续检查",
        evidence_text="继续检查",
        observed_evidence_text="继续检查",
    )
    with pytest.raises(AriadneError) as forged:
        _run(
            registry.invoke(
                "conversation_state",
                {
                    "action": "apply",
                    "evidence_text": "测试已经通过",
                    "operations": [
                        {
                            "op": "set_status",
                            "entity_id": "session:current_goal",
                            "status": "done",
                            "authority": "user_explicit",
                            "evidence_quote": "测试已经通过",
                        }
                    ],
                },
                ctx,
            )
        )
    assert forged.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"

    with pytest.raises(AriadneError) as terminal:
        _run(
            registry.invoke(
                "conversation_state",
                {
                    "action": "apply",
                    "operations": [
                        {
                            "op": "set_status",
                            "entity_id": "session:current_goal",
                            "status": "done",
                            "evidence_quote": "继续检查",
                        }
                    ],
                },
                ctx,
            )
        )
    assert terminal.value.error.code == "ARIADNE_TOOL_DENIED"
    _goal_id, goal = _current_goal(memory, "state-authority")
    assert goal["status"] == "active"


def test_verified_goal_cannot_be_reactivated_by_model_facing_state_tool(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="verified-reactivation",
        turn_id="t1",
        user="目标是完成安全检查。",
    )
    _capture(
        memory,
        session_id="verified-reactivation",
        turn_id="t2",
        user="继续",
        verified_goal={
            "status": "completed",
            "task_id": "task-security",
            "goal": "完成安全检查",
            "summary": "安全检查已验证通过",
            "check_ids": ["security-check"],
        },
    )
    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    ctx = ToolContext(
        session_id="verified-reactivation",
        turn_id="t3",
        sandbox=None,
        memory=memory,
        user_text="谢谢",
        evidence_text="谢谢",
        observed_evidence_text="谢谢",
    )

    with pytest.raises(AriadneError) as model_write:
        _run(
            registry.invoke(
                "conversation_state",
                {
                    "action": "apply",
                    "operations": [
                        {
                            "op": "set_status",
                            "entity_id": "session:current_goal",
                            "status": "active",
                            "evidence_quote": "谢谢",
                        }
                    ],
                },
                ctx,
            )
        )
    assert model_write.value.error.code == "ARIADNE_TOOL_DENIED"

    goal_id, _goal = _current_goal(memory, "verified-reactivation")
    with pytest.raises(AriadneError) as lower_authority:
        memory.state.apply_ops(
            session_id="verified-reactivation",
            source_turn_id="t3",
            evidence_text="谢谢",
            operations=[
                {
                    "op": "set_status",
                    "entity_id": goal_id,
                    "status": "active",
                    "authority": "user_explicit",
                    "evidence_quote": "谢谢",
                }
            ],
        )
    assert lower_authority.value.error.code == "ARIADNE_MEMORY_CONFLICT"
    _goal_id, goal = _current_goal(memory, "verified-reactivation")
    assert goal["status"] == "done"
    assert goal["status_authority"] == "verified_check"
    episode = memory.episodes.for_turn(
        session_id="verified-reactivation",
        turn_id="t2",
    )
    assert episode is not None and episode["status"] == "completed"


def test_structured_tool_secrets_are_redacted_before_episode_persistence(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    raw = {
        "api_key": "ABCDEF123456",
        "nested": {"password": "hunter123456"},
    }
    assert redact_secrets(raw) == {
        "api_key": "***",
        "nested": {"password": "***"},
    }

    _capture(
        memory,
        session_id="secrets",
        turn_id="t1",
        user="继续",
        tool_calls=[
            {
                "call_id": "call-secret",
                "name": "example_api",
                "arguments": raw,
                "output": {"status": "ok", "token": "OUTPUTTOKEN123456"},
                "status": "completed",
            }
        ],
    )

    persisted = "\n".join(
        [
            memory.episodes.path.read_text(encoding="utf-8"),
            memory.capture_journal.path.read_text(encoding="utf-8"),
        ]
    )
    assert "ABCDEF123456" not in persisted
    assert "hunter123456" not in persisted
    assert "OUTPUTTOKEN123456" not in persisted
    episode = memory.episodes.for_turn(session_id="secrets", turn_id="t1")
    assert episode is not None
    attempt = next(event for event in episode["events"] if event["type"] == "attempt")
    assert "arguments" not in attempt["metadata"]
    assert attempt["metadata"]["arguments_sha256"]


def test_camel_case_and_nested_allowlisted_tool_secrets_are_digest_only(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    payload = {
        "status": {
            "secretKey": "SECRETKEY123456",
            "authToken": "AUTHTOKEN123456",
            "sessionToken": "SESSIONTOKEN123456",
        }
    }
    assert redact_secrets(payload) == {
        "status": {
            "secretKey": "***",
            "authToken": "***",
            "sessionToken": "***",
        }
    }

    _capture(
        memory,
        session_id="nested-secrets",
        turn_id="t1",
        user="继续",
        tool_calls=[
            {
                "call_id": "nested-secret-call",
                "name": "nested_secret_tool",
                "arguments": {},
                "output": payload,
                "status": "completed",
            }
        ],
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "memory").rglob("*.json")
    )
    for secret in (
        "SECRETKEY123456",
        "AUTHTOKEN123456",
        "SESSIONTOKEN123456",
    ):
        assert secret not in persisted
    episode = memory.episodes.for_turn(
        session_id="nested-secrets",
        turn_id="t1",
    )
    assert episode is not None
    observation = next(
        event for event in episode["events"] if event["type"] == "observation"
    )
    assert "retained by digest only" in observation["content"]


@pytest.mark.parametrize(
    ("field", "assignment", "secret"),
    [
        ("status", "authToken=AUTHTOKEN123456", "AUTHTOKEN123456"),
        ("ok", "sessionToken=OKTOKEN123456", "OKTOKEN123456"),
        ("success", "secretKey=SUCCESSTOKEN123456", "SUCCESSTOKEN123456"),
        ("exit_code", "authToken=EXITTOKEN123456", "EXITTOKEN123456"),
        ("returncode", "sessionToken=RETURNTOKEN123456", "RETURNTOKEN123456"),
        ("count", "secretKey=COUNTTOKEN123456", "COUNTTOKEN123456"),
        ("path", "sessionToken: SESSIONTOKEN123456", "SESSIONTOKEN123456"),
        ("changed", "secretKey=SECRETKEY123456", "SECRETKEY123456"),
        ("passed", "authToken=PASSEDTOKEN123456", "PASSEDTOKEN123456"),
        ("failed", "secretKey=FAILEDTOKEN123456", "FAILEDTOKEN123456"),
    ],
)
def test_allowlisted_scalar_camel_case_secret_assignments_are_redacted(
    tmp_path: Path,
    field: str,
    assignment: str,
    secret: str,
) -> None:
    assert secret not in redact_text(assignment)
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id=f"scalar-secret-{field}",
        turn_id="t1",
        user="继续",
        tool_calls=[
            {
                "call_id": f"scalar-secret-{field}",
                "name": "scalar_secret_tool",
                "arguments": {},
                "output": {field: assignment},
                "status": "completed",
            }
        ],
    )

    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "memory").rglob("*.json")
    )
    assert secret not in persisted
    assert "***" in persisted


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ("githubToken=GITHUBTOKEN123456", "GITHUBTOKEN123456"),
        ("csrfToken=CSRFTOKEN123456", "CSRFTOKEN123456"),
        ("awsSecretAccessKey=AWSSECRET123456", "AWSSECRET123456"),
        ("consumerSecret=CONSUMERSECRET123456", "CONSUMERSECRET123456"),
        (
            "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
            "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        ),
        ("authorization=Token abcdefghijkl", "abcdefghijkl"),
        ("Authorization: ApiKey shortkey", "shortkey"),
        ("Authorization: Bearer abcdef", "abcdef"),
    ],
)
def test_generic_scalar_credentials_never_reach_persistent_memory(
    tmp_path: Path,
    payload: str,
    secret: str,
) -> None:
    assert secret not in redact_text(payload)
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="generic-scalar-secret",
        turn_id="t1",
        user="继续",
        tool_calls=[
            {
                "call_id": "generic-secret",
                "name": "generic_secret_tool",
                "arguments": {},
                "output": {"status": payload},
                "status": "completed",
            }
        ],
    )
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "memory").rglob("*.json")
    )
    assert secret not in persisted


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ('password="correct horse battery staple"', "correct horse battery staple"),
        ("password='correct horse battery staple'", "correct horse battery staple"),
        ("password=correct horse battery staple", "horse battery"),
        (
            "status=ok password=correct horse battery staple",
            "correct horse battery staple",
        ),
        ("result=success accessToken=ACCESS123456", "ACCESS123456"),
        (
            "nested_secret_tool completed: password=PASSWORD123456",
            "PASSWORD123456",
        ),
        ("API Key: supersecret123", "supersecret123"),
        ("Access Token: abcdefghijklmnop", "abcdefghijklmnop"),
        ("AWS Secret Access Key: AWSSECRET123456", "AWSSECRET123456"),
    ],
)
def test_quoted_and_spaced_secret_assignments_never_reach_memory(
    tmp_path: Path,
    payload: str,
    secret: str,
) -> None:
    assert secret not in redact_text(payload)
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="quoted-secret",
        turn_id="t1",
        user="继续",
        tool_calls=[
            {
                "call_id": "quoted-secret",
                "name": "quoted_secret_tool",
                "arguments": {},
                "output": {"status": payload},
                "status": "completed",
            }
        ],
    )
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "memory").rglob("*.json")
    )
    assert secret not in persisted
    assert secret not in redact_text(payload)


def test_legacy_pending_with_identity_but_empty_prepared_is_quarantined(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "shared" / "capture_journal.json"
    journal_path.parent.mkdir(parents=True)
    capture_id = CaptureJournalStore.capture_id(
        workspace_key="/legacy/workspace",
        session_id="legacy-session",
        turn_id="legacy-turn",
    )
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    capture_id: {
                        "capture_id": capture_id,
                        "workspace_key": "/legacy/workspace",
                        "session_id": "legacy-session",
                        "turn_id": "legacy-turn",
                        "input_digest": "legacy-digest",
                        "state_store_identity": "conversation-state-v1:deadbeef",
                        "status": "in_progress",
                        "prepared": {},
                        "stages": {
                            stage: {"status": "pending", "result": {}}
                            for stage in (
                                "user_model",
                                "state",
                                "episode",
                                "reflection",
                                "prospective",
                            )
                        },
                        "report": {},
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    journal = CaptureJournalStore(journal_path)
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert capture_id not in raw["records"]
    quarantined = journal.list_quarantined()
    assert len(quarantined) == 1
    assert quarantined[0]["status"] == "migration_required"
    assert "prepared" in quarantined[0]["migration_reason"]
    assert journal.list_pending(workspace_key="/legacy/workspace") == []


def test_legacy_completed_without_identity_replays_by_digest(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "shared" / "capture_journal.json"
    journal_path.parent.mkdir(parents=True)
    workspace_key = ""
    session_id = "legacy-done"
    turn_id = "t-done"
    capture_id = CaptureJournalStore.capture_id(
        workspace_key=workspace_key,
        session_id=session_id,
        turn_id=turn_id,
    )
    report = {"status": "used", "episode_id": "ep-legacy", "event_ids": []}
    # Build digest the same way capture_turn does for empty tools.
    from ariadne.memory.auto_capture import AutomaticMemoryProjector

    memory = Memory.local(tmp_path / "memory")
    # First complete a real capture so we know a valid digest/report path,
    # then rewrite the journal row as legacy completed without identity.
    real = _capture(
        memory,
        session_id=session_id,
        turn_id=turn_id,
        user="继续",
    )
    assert real["status"] in {"used", "skipped"}
    row = memory.capture_journal.get(
        workspace_key=workspace_key,
        session_id=session_id,
        turn_id=turn_id,
    )
    assert row is not None
    digest = row["input_digest"]
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    capture_id: {
                        "capture_id": capture_id,
                        "workspace_key": workspace_key,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "input_digest": digest,
                        "status": "completed",
                        "prepared": {"events": []},
                        "stages": {
                            stage: {"status": "done", "result": {}}
                            for stage in (
                                "user_model",
                                "state",
                                "episode",
                                "reflection",
                                "prospective",
                            )
                        },
                        "report": report,
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # Rebind stores to the rewritten v1 file (migrates on open).
    memory.capture_journal = CaptureJournalStore(journal_path)
    memory.auto_capture.journal = memory.capture_journal
    replayed = _capture(
        memory,
        session_id=session_id,
        turn_id=turn_id,
        user="继续",
    )
    assert replayed.get("idempotent_replay") is True or replayed.get(
        "episode_id"
    ) == "ep-legacy"
    assert memory.capture_journal.list_pending(workspace_key=workspace_key) == []


def test_legacy_pending_capture_without_affinity_is_quarantined_once(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "shared" / "capture_journal.json"
    journal_path.parent.mkdir(parents=True)
    capture_id = CaptureJournalStore.capture_id(
        workspace_key="/legacy/workspace",
        session_id="legacy-session",
        turn_id="legacy-turn",
    )
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": {
                    capture_id: {
                        "capture_id": capture_id,
                        "workspace_key": "/legacy/workspace",
                        "session_id": "legacy-session",
                        "turn_id": "legacy-turn",
                        "input_digest": "legacy-digest",
                        "status": "in_progress",
                        "prepared": {"events": []},
                        "stages": {},
                        "report": {},
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    journal = CaptureJournalStore(journal_path)
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 2
    assert capture_id not in raw["records"]
    quarantined = journal.list_quarantined()
    assert len(quarantined) == 1
    assert quarantined[0]["capture_id"] == capture_id
    assert quarantined[0]["status"] == "migration_required"
    assert quarantined[0]["migration_error_code"] == (
        "ARIADNE_MEMORY_CAPTURE_MIGRATION_REQUIRED"
    )
    assert journal.list_pending(workspace_key="/legacy/workspace") == []

    memory = Memory.local(tmp_path / "memory")
    memory.capture_journal = journal
    memory.auto_capture.journal = journal
    first = _capture(
        memory,
        session_id="new-session",
        turn_id="new-turn-1",
        user="继续",
    )
    second = _capture(
        memory,
        session_id="new-session",
        turn_id="new-turn-2",
        user="继续",
    )
    assert first["recovery_failures"] == []
    assert second["recovery_failures"] == []
    after = journal.list_quarantined()[0]
    assert after["status"] == "migration_required"
    assert int(after.get("resume_attempts") or 0) == 0


def test_v2_pending_capture_with_invalid_event_is_quarantined_before_recovery(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "shared" / "capture_journal.json"
    journal_path.parent.mkdir(parents=True)
    workspace_key = "/workspace/invalid-event"
    session_id = "invalid-event-session"
    turn_id = "t1"
    capture_id = CaptureJournalStore.capture_id(
        workspace_key=workspace_key,
        session_id=session_id,
        turn_id=turn_id,
    )
    stages = {
        stage: {"status": "pending", "result": {}}
        for stage in (
            "user_model",
            "state",
            "episode",
            "reflection",
            "prospective",
        )
    }
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "records": {
                    capture_id: {
                        "capture_id": capture_id,
                        "workspace_key": workspace_key,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "input_digest": "digest",
                        "state_store_identity": "conversation-state-v1:test",
                        "status": "in_progress",
                        "prepared": {"events": [1]},
                        "stages": stages,
                        "report": {},
                    }
                },
                "quarantined_records": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    journal = CaptureJournalStore(journal_path)
    raw = json.loads(journal_path.read_text(encoding="utf-8"))
    assert capture_id not in raw["records"]
    quarantined = journal.list_quarantined(workspace_key=workspace_key)
    assert len(quarantined) == 1
    assert quarantined[0]["status"] == "migration_required"
    assert "event 0" in quarantined[0]["migration_reason"]
    assert journal.list_pending(workspace_key=workspace_key) == []


def test_terminal_task_without_host_binding_fails_without_pointer_fallback(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="binding-required",
        turn_id="t1",
        user="目标是完成任务 A。",
    )
    _capture(
        memory,
        session_id="binding-required",
        turn_id="t2",
        user="新目标是完成任务 B。",
    )

    with pytest.raises(AriadneError) as error:
        _run(
            memory.capture_turn(
                session_id="binding-required",
                turn_id="t3",
                user_text="继续",
                assistant_text="好的。",
                verified_goal={
                    "status": "completed",
                    "task_id": "task-a",
                    "goal": "完成任务 A",
                    "summary": "任务 A 已验证完成",
                    "check_ids": ["check-a"],
                },
            )
        )
    assert error.value.error.code == "ARIADNE_MEMORY_GOAL_BINDING"
    state = memory.state.get("binding-required")
    assert state["entities"]["goal:t2"]["status"] == "active"
    assert memory.state.current_goal_id("binding-required") == "goal:t2"

    # The permanent binding error is quarantined on the next bounded recovery,
    # rather than rotating forever as a transient failure.
    report = _capture(
        memory,
        session_id="binding-required",
        turn_id="t4",
        user="继续",
    )
    assert report["recovery_failures"] == []
    assert report["migration_required_capture_ids"]


def test_host_task_goal_binding_is_immutable_and_not_model_writable(
    tmp_path: Path,
) -> None:
    assert redact_text("task-security") == "task-security"
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="binding-api",
        turn_id="t1",
        user="目标是完成安全检查。",
    )
    memory.state.bind_task_goal(
        session_id="binding-api",
        task_id="task-security",
        goal_id="goal:t1",
        source_turn_id="host-t1",
        evidence_text="目标是完成安全检查。",
    )
    assert memory.state.goal_id_for_task("binding-api", "task-security") == "goal:t1"
    with pytest.raises(AriadneError) as conflict:
        memory.state.bind_task_goal(
            session_id="binding-api",
            task_id="task-security",
            goal_id="goal:t2",
            source_turn_id="host-t2",
            evidence_text="目标是完成安全检查。",
        )
    assert conflict.value.error.code == "ARIADNE_MEMORY_GOAL_BINDING"

    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    ctx = ToolContext(
        session_id="binding-api",
        turn_id="t2",
        sandbox=None,
        memory=memory,
        user_text="写入绑定",
        evidence_text="写入绑定",
        observed_evidence_text="写入绑定",
    )
    with pytest.raises(AriadneError) as denied:
        _run(
            registry.invoke(
                "conversation_state",
                {
                    "action": "apply",
                    "operations": [
                        {
                            "op": "set_attribute",
                            "entity_id": "goal:t1",
                            "key": "task_id",
                            "value": "forged-task",
                            "evidence_quote": "写入绑定",
                        }
                    ],
                },
                ctx,
            )
        )
    assert denied.value.error.code == "ARIADNE_TOOL_DENIED"


def test_same_turn_task_goal_completion_stays_one_completed_episode(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    # Host binds the task at plan creation before the first capture. The goal
    # entity itself is created by this same turn's user goal event.
    memory.state.bind_task_goal(
        session_id="same-turn-complete",
        task_id="task-same-turn",
        goal_id="goal:t1",
        source_turn_id="t1",
        evidence_text="目标是完成一次检查。",
    )
    report = _capture(
        memory,
        session_id="same-turn-complete",
        turn_id="t1",
        user="目标是完成一次检查。",
        verified_goal={
            "status": "completed",
            "task_id": "task-same-turn",
            "goal": "完成一次检查",
            "summary": "检查已验证完成",
            "check_ids": ["check-same-turn"],
        },
    )
    assert report["status"] == "used"
    state = memory.state.get("same-turn-complete")
    assert state["entities"]["goal:t1"]["status"] == "done"
    episode = memory.episodes.for_turn(
        session_id="same-turn-complete",
        turn_id="t1",
    )
    assert episode is not None
    assert episode["status"] == "completed"
    assert memory.episodes.for_turn_segment(
        session_id="same-turn-complete",
        turn_id="t1",
        segment="close",
    ) is None


def test_unredacted_traces_do_not_authorize_secret_persistence_in_memory(
    tmp_path: Path,
) -> None:
    memory_root = tmp_path / "memory"
    memory = Memory.local(memory_root)
    skills = SkillStore({})
    registry = build_default_registry(memory=memory, skills=skills)

    async def secret_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {"status": "ok", "token": "LONGTERMSECRET123456"}

    registry.register(
        ToolSpec(
            name="secret_test_tool",
            description="Return a structured test credential.",
            parameters={"type": "object", "additionalProperties": False},
            handler=secret_tool,
            side_effect_level="read",
            network_access="none",
            idempotent=True,
        )
    )
    calls = {"count": 0}

    def script(
        messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "secret-call",
                        "type": "function",
                        "function": {
                            "name": "secret_test_tool",
                            "arguments": "{}",
                        },
                    }
                ],
            }
        return {"content": "完成。"}

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TurnApplication(
        model=FakeModel(script=script),
        tools=registry,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(
            workspace=workspace,
            data_dir=tmp_path / "data",
        ),
        task_mode_policy="off",
        redact_traces=False,
    )
    result = _run(app.run(prompt="运行测试工具", session_id="secret-turn"))

    assert result.tool_calls[0].output["token"] == "LONGTERMSECRET123456"
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in memory_root.rglob("*.json")
    )
    assert "LONGTERMSECRET123456" not in persisted


def test_capture_journal_resumes_after_cross_store_failure_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    for index in (1, 2):
        _capture(
            memory,
            session_id=f"review-{index}",
            turn_id=f"t{index}",
            user="代码 review 时先看测试覆盖。",
        )

    original = ReflectionStore.observe
    injected = {"failed": False}

    def fail_once(self: ReflectionStore, **kwargs: Any) -> list[dict[str, Any]]:
        if self is memory.reflection and not injected["failed"]:
            injected["failed"] = True
            raise RuntimeError("injected reflection failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ReflectionStore, "observe", fail_once)
    text = (
        "以后 Python 项目都用 uv，不用 poetry 了；"
        "代码 review 时先看测试覆盖。"
    )
    with pytest.raises(RuntimeError, match="injected reflection failure"):
        _capture(
            memory,
            session_id="review-3",
            turn_id="t3",
            user=text,
        )

    assert memory.capture_journal is not None
    partial = memory.capture_journal.get(
        workspace_key="",
        session_id="review-3",
        turn_id="t3",
    )
    assert partial is not None
    assert partial["stages"]["episode"]["status"] == "done"
    assert partial["stages"]["reflection"]["status"] == "pending"

    replay = _capture(
        memory,
        session_id="review-3",
        turn_id="t3",
        user=text,
    )
    assert replay["status"] == "used"
    preference = next(
        row
        for row in memory.user_model.list()
        if row["key"] == "python_package_manager"
    )
    assert preference["revision"] == 1
    pending = memory.reflection.list(status="pending")
    review = next(row for row in pending if row["key"] == "review_order")
    assert review["session_count"] == 3
    completed = memory.capture_journal.get(
        workspace_key="",
        session_id="review-3",
        turn_id="t3",
    )
    assert completed is not None and completed["status"] == "completed"


def test_next_turn_automatically_resumes_pending_capture_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    for index in (1, 2):
        _capture(
            memory,
            session_id=f"review-{index}",
            turn_id=f"t{index}",
            user="代码 review 时先看测试覆盖。",
        )

    original = ReflectionStore.observe
    injected = {"failed": False}

    def fail_once(self: ReflectionStore, **kwargs: Any) -> list[dict[str, Any]]:
        if self is memory.reflection and not injected["failed"]:
            injected["failed"] = True
            raise RuntimeError("turn lifecycle reflection failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ReflectionStore, "observe", fail_once)
    skills = SkillStore({})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TurnApplication(
        model=FakeModel(script=lambda messages, tool_payload: {"content": "好的。"}),
        tools=build_default_registry(memory=memory, skills=skills),
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(
            workspace=workspace,
            data_dir=tmp_path / "data",
        ),
        task_mode_policy="off",
    )

    failed_turn = _run(
        app.run(
            prompt="代码 review 时先看测试覆盖。",
            session_id="review-3",
        )
    )
    failed_layer = next(
        row for row in failed_turn.memory.layers if row.name == "auto_capture"
    )
    assert failed_layer.status == "failed"
    pending = memory.capture_journal.get(
        workspace_key="",
        session_id="review-3",
        turn_id=failed_turn.turn_id,
    )
    assert pending is not None and pending["status"] == "in_progress"

    next_turn = _run(app.run(prompt="继续", session_id="review-3"))
    recovered = memory.capture_journal.get(
        workspace_key="",
        session_id="review-3",
        turn_id=failed_turn.turn_id,
    )
    assert recovered is not None and recovered["status"] == "completed"
    next_layer = next(
        row for row in next_turn.memory.layers if row.name == "auto_capture"
    )
    assert "recovered=1" in next_layer.notes
    assert recovered["capture_id"] in next_layer.item_ids
    candidate = next(
        row
        for row in memory.reflection.list(status="pending")
        if row["key"] == "review_order"
    )
    assert candidate["session_count"] == 3


def test_pending_capture_recovery_is_scoped_to_its_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = Memory.local(tmp_path / "shared-user-memory")
    state_a = ConversationStateStore(tmp_path / "workspace-a" / "state.json")
    state_b = ConversationStateStore(tmp_path / "workspace-b" / "state.json")
    projector_a = AutomaticMemoryProjector(
        episodes=shared.episodes,
        user_model=shared.user_model,
        journal=shared.capture_journal,
        state=state_a,
        reflection=shared.reflection,
        prospective=shared.prospective,
    )
    projector_b = AutomaticMemoryProjector(
        episodes=shared.episodes,
        user_model=shared.user_model,
        journal=shared.capture_journal,
        state=state_b,
        reflection=shared.reflection,
        prospective=shared.prospective,
    )
    original = ConversationStateStore.apply_ops
    injected = {"failed": False}

    def fail_state_a_once(self: ConversationStateStore, **kwargs: Any) -> dict[str, Any]:
        if self is state_a and not injected["failed"]:
            injected["failed"] = True
            raise RuntimeError("workspace A state failure")
        return original(self, **kwargs)

    monkeypatch.setattr(ConversationStateStore, "apply_ops", fail_state_a_once)
    with pytest.raises(RuntimeError, match="workspace A state failure"):
        _run(
            projector_a.capture_turn(
                session_id="session-a",
                turn_id="t1",
                workspace_key="/workspace/a",
                user_text="目标是修复 A。",
                assistant_text="好的。",
            )
        )

    pending = shared.capture_journal.get(
        workspace_key="/workspace/a",
        session_id="session-a",
        turn_id="t1",
    )
    assert pending is not None and pending["status"] == "in_progress"
    report_b = _run(
        projector_b.capture_turn(
            session_id="session-b",
            turn_id="t1",
            workspace_key="/workspace/b",
            user_text="继续",
            assistant_text="好的。",
        )
    )
    assert report_b["recovered_capture_ids"] == []
    assert "session-a" not in (state_b._read().get("documents") or {})
    pending = shared.capture_journal.get(
        workspace_key="/workspace/a",
        session_id="session-a",
        turn_id="t1",
    )
    assert pending is not None and pending["status"] == "in_progress"

    report_a = _run(
        projector_a.capture_turn(
            session_id="session-a",
            turn_id="t2",
            workspace_key="/workspace/a",
            user_text="继续",
            assistant_text="好的。",
        )
    )
    assert pending["capture_id"] in report_a["recovered_capture_ids"]
    state_a_snapshot = state_a.get("session-a")
    goal_id = state_a.current_goal_id_from_state(state_a_snapshot)
    assert goal_id is not None
    assert (
        state_a_snapshot["entities"][goal_id]["attributes"]["description"][
            "value"
        ]
        == "修复 A"
    )
    assert "session-a" not in (state_b._read().get("documents") or {})


def test_pending_capture_recovery_validates_state_store_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = Memory.local(tmp_path / "shared-user-memory")
    state_original = ConversationStateStore(tmp_path / "original" / "state.json")
    state_reconfigured = ConversationStateStore(
        tmp_path / "reconfigured" / "state.json"
    )
    original_projector = AutomaticMemoryProjector(
        episodes=shared.episodes,
        user_model=shared.user_model,
        journal=shared.capture_journal,
        state=state_original,
        reflection=shared.reflection,
        prospective=shared.prospective,
    )
    reconfigured_projector = AutomaticMemoryProjector(
        episodes=shared.episodes,
        user_model=shared.user_model,
        journal=shared.capture_journal,
        state=state_reconfigured,
        reflection=shared.reflection,
        prospective=shared.prospective,
    )
    original_apply = ConversationStateStore.apply_ops
    injected = {"failed": False}

    def fail_original_once(self: ConversationStateStore, **kwargs: Any) -> dict[str, Any]:
        if self is state_original and not injected["failed"]:
            injected["failed"] = True
            raise RuntimeError("original state failure")
        return original_apply(self, **kwargs)

    monkeypatch.setattr(ConversationStateStore, "apply_ops", fail_original_once)
    with pytest.raises(RuntimeError, match="original state failure"):
        _run(
            original_projector.capture_turn(
                session_id="affinity-session",
                turn_id="t1",
                workspace_key="/same/workspace",
                user_text="目标是修复 affinity。",
                assistant_text="好的。",
            )
        )

    report = _run(
        reconfigured_projector.capture_turn(
            session_id="other-session",
            turn_id="t2",
            workspace_key="/same/workspace",
            user_text="继续",
            assistant_text="好的。",
        )
    )
    assert report["status"] == "failed"
    assert report["recovered_capture_ids"] == []
    assert report["recovery_failures"][0]["error_code"] == (
        "ARIADNE_MEMORY_CAPTURE_AFFINITY"
    )
    assert "affinity-session" not in (
        state_reconfigured._read().get("documents") or {}
    )
    pending = shared.capture_journal.get(
        workspace_key="/same/workspace",
        session_id="affinity-session",
        turn_id="t1",
    )
    assert pending is not None and pending["status"] == "in_progress"
    assert pending["resume_attempts"] == 1


def test_capture_journal_failed_resume_rotates_pending_records(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    journal = memory.capture_journal
    for index in (1, 2):
        journal.start(
            workspace_key="",
            session_id=f"rotation-{index}",
            turn_id=f"t{index}",
            input_digest=f"digest-{index}",
            state_store_identity=memory.state.store_identity,
            prepared={"events": []},
        )

    initial = journal.list_pending(workspace_key="", limit=2)
    assert len(initial) == 2
    failed_id = initial[0]["capture_id"]
    next_id = initial[1]["capture_id"]
    journal.note_resume_failure(
        capture_id=failed_id,
        error_code="ARIADNE_MEMORY_CAPTURE_RESUME_FAILED",
        error_message="injected recovery failure",
    )

    assert journal.list_pending(workspace_key="", limit=1)[0]["capture_id"] == next_id
    failed = next(
        row
        for row in journal.list_pending(workspace_key="", limit=2)
        if row["capture_id"] == failed_id
    )
    assert failed["resume_attempts"] == 1
    assert failed["last_resume_failure"]["error_message"] == "injected recovery failure"


def test_pending_recovery_failure_is_visible_without_failing_current_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    for index in (1, 2):
        _capture(
            memory,
            session_id=f"persistent-{index}",
            turn_id=f"t{index}",
            user="代码 review 时先看测试覆盖。",
        )

    def always_fail(self: ReflectionStore, **kwargs: Any) -> list[dict[str, Any]]:
        if self is memory.reflection:
            raise RuntimeError("persistent reflection failure")
        return []

    monkeypatch.setattr(ReflectionStore, "observe", always_fail)
    with pytest.raises(RuntimeError, match="persistent reflection failure"):
        _capture(
            memory,
            session_id="persistent-3",
            turn_id="t3",
            user="代码 review 时先看测试覆盖。",
        )
    pending = memory.capture_journal.get(
        workspace_key="",
        session_id="persistent-3",
        turn_id="t3",
    )
    assert pending is not None and pending["status"] == "in_progress"

    skills = SkillStore({})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = TurnApplication(
        model=FakeModel(script=lambda messages, tool_payload: {"content": "好的。"}),
        tools=build_default_registry(memory=memory, skills=skills),
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(
            workspace=workspace,
            data_dir=tmp_path / "data",
        ),
        task_mode_policy="off",
    )

    result = _run(app.run(prompt="继续", session_id="persistent-3"))
    layer = next(row for row in result.memory.layers if row.name == "auto_capture")
    assert result.status == "completed"
    assert layer.status == "failed"
    assert "capture=skipped" in layer.notes
    assert "recovery_failed=1" in layer.notes
    pending = memory.capture_journal.get(
        workspace_key="",
        session_id="persistent-3",
        turn_id="t3",
    )
    assert pending is not None and pending["status"] == "in_progress"
    assert pending["resume_attempts"] == 1


def test_failed_attempt_stays_in_same_episode_until_verified_terminal_outcome(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    _capture(
        memory,
        session_id="retry",
        turn_id="t1",
        user="目标是修复登录问题。",
    )
    _capture(
        memory,
        session_id="retry",
        turn_id="t2",
        user="继续排查。",
        tool_calls=[
            {
                "call_id": "failed-call",
                "name": "diagnose_login",
                "arguments": {},
                "output": {"error": "timeout"},
                "status": "failed",
            }
        ],
    )
    _capture(
        memory,
        session_id="retry",
        turn_id="t3",
        user="尝试另一个方案。",
    )

    episodes = memory.episodes.list(session_id="retry")
    assert len(episodes) == 1
    assert episodes[0]["related_turn_ids"] == ["t1", "t2", "t3"]
    assert episodes[0]["status"] == "active"
    assert any(event["type"] == "error" for event in episodes[0]["events"])


def test_episode_search_and_expansion_enforce_total_evidence_bytes(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")
    events = [
        _event(
            "observation",
            f"needle event {index} " + ("x" * 1800),
            session_id="large",
            turn_id="t-large",
        )
        for index in range(70)
    ]
    episode = memory.episodes.append_turn(
        session_id="large",
        turn_id="t-large",
        workspace_key="",
        events=events,
    )

    result = _run(
        memory.memory_search(
            query="needle",
            session_id="large",
            scope="session",
            mode="fast",
        )
    )
    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 64_000
    hit = result["hits"][0]
    assert len(hit["event_chain"]) <= 7
    assert len(hit["event_ids"]) == 70
    assert hit["evidence_page"]["has_more"] is True

    page = memory.expand_episode_evidence(
        episode_id=episode["episode_id"],
        session_id="large",
        scope="session",
        limit=16,
    )
    assert len(json.dumps(page, ensure_ascii=False).encode("utf-8")) <= 16_000
    assert page["has_more"] is True
    assert page["next_after_event_id"] == page["events"][-1]["event_id"]
    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    assert "memory_expand_evidence" in registry.tools


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        '{"events":{}}',
        '{"events":[{"type":"outcome"}]}',
    ],
)
def test_llm_capture_protocol_errors_fastfail(body: str) -> None:
    model = FakeModel(script=lambda messages, tool_payload: {"content": body})
    extractor = make_llm_memory_extractor(model)
    with pytest.raises(AriadneError) as caught:
        _run(extractor({"user_text": "之前那个设置"}))
    assert caught.value.error.code == "ARIADNE_MEMORY_CAPTURE_PROTOCOL"


def test_llm_capture_explicit_empty_result_is_valid() -> None:
    model = FakeModel(
        script=lambda messages, tool_payload: {"content": '{"events":[]}'}
    )
    extractor = make_llm_memory_extractor(model)
    assert _run(extractor({"user_text": "之前那个设置"})) == []


def test_unknown_capture_status_is_reported_as_failed_memory_layer(
    tmp_path: Path,
) -> None:
    memory = Memory.local(tmp_path / "memory")

    async def invalid_capture(**kwargs: Any) -> dict[str, Any]:
        return {"status": "mystery"}

    memory.capture_turn = invalid_capture  # type: ignore[method-assign]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = SkillStore({})
    app = TurnApplication(
        model=FakeModel(script=lambda messages, tool_payload: {"content": "完成。"}),
        tools=build_default_registry(memory=memory, skills=skills),
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(
            workspace=workspace,
            data_dir=tmp_path / "data",
        ),
        task_mode_policy="off",
    )

    result = _run(app.run(prompt="继续", session_id="invalid-capture"))
    layer = next(row for row in result.memory.layers if row.name == "auto_capture")
    assert result.status == "completed"
    assert layer.status == "failed"
    assert "ARIADNE_MEMORY_CAPTURE_PROTOCOL" in layer.notes
