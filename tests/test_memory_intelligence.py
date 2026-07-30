from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import EvidenceRef, Memory
from ariadne.memory.state import ConversationStateStore
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolContext, build_default_registry


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
) -> dict[str, Any]:
    return _run(
        memory.capture_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_text=user,
            assistant_text=assistant,
            tool_calls=tool_calls or [],
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
        user_model=memory.user_model,
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

    accepted_ctx = ToolContext(
        session_id="s4",
        turn_id="t5",
        sandbox=None,
        memory=memory,
        user_text="我同意接受这个建议，设为长期偏好。",
    )
    accepted = _run(
        registry.invoke(
            "memory_reflection",
            {"action": "accept", "candidate_id": candidate_id},
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
