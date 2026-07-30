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
    state = memory.state.get("login")
    assert state["entities"]["session:current_goal"]["attributes"]["description"][
        "value"
    ] == "修复登录超时"
    assert state["entities"]["session:current_goal"]["status"] == "done"
    assert state["entities"]["session:current_goal"]["status_authority"] == "verified_check"


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

    goal = memory.state.get("goal")["entities"]["session:current_goal"]
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

    goal = memory.state.get("verified-goal")["entities"]["session:current_goal"]
    assert goal["status"] == "done"
    assert goal["status_authority"] == "verified_check"


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

    goal = memory.state.get("free-text-goal")["entities"]["session:current_goal"]
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
    assert (
        memory.state.get("state-authority")["entities"]["session:current_goal"][
            "status"
        ]
        == "active"
    )


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

    with pytest.raises(AriadneError) as lower_authority:
        memory.state.apply_ops(
            session_id="verified-reactivation",
            source_turn_id="t3",
            evidence_text="谢谢",
            operations=[
                {
                    "op": "set_status",
                    "entity_id": "session:current_goal",
                    "status": "active",
                    "authority": "user_explicit",
                    "evidence_quote": "谢谢",
                }
            ],
        )
    assert lower_authority.value.error.code == "ARIADNE_MEMORY_CONFLICT"
    goal = memory.state.get("verified-reactivation")["entities"][
        "session:current_goal"
    ]
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
        ("path", "sessionToken: SESSIONTOKEN123456", "SESSIONTOKEN123456"),
        ("changed", "secretKey=SECRETKEY123456", "SECRETKEY123456"),
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
    assert (
        state_a.get("session-a")["entities"]["session:current_goal"][
            "attributes"
        ]["description"]["value"]
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
