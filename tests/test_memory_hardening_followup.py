from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ariadne.config import load_settings
from ariadne.errors import AriadneError
from ariadne.memory import Memory, MemoryLimits, is_goal_id, make_goal_id
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolContext, build_default_registry
from ariadne.redact import redact_text


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize(
    ("value", "secret"),
    [
        ("Secret Access Key: SECRET123456", "SECRET123456"),
        ("Cloud Secret Access Key: CLOUD123456", "CLOUD123456"),
        ("accessKey=ACCESS123456", "ACCESS123456"),
        ("sessionKey=SESSION123456", "SESSION123456"),
        ("ghp_abcdefghijklmnopqrstuvwx", "ghp_abcdefghijklmnopqrstuvwx"),
        ("xoxb-1234567890-abcdefghijkl", "xoxb-1234567890-abcdefghijkl"),
        ("hf_abcd1234567890", "hf_abcd1234567890"),
        ("xai-abcd1234567890", "xai-abcd1234567890"),
        ("AIzaabcd1234567890", "AIzaabcd1234567890"),
        ("npm_abcd1234567890", "npm_abcd1234567890"),
    ],
)
def test_generic_provider_credentials_are_redacted(value: str, secret: str) -> None:
    assert secret not in redact_text(value)


def test_conversation_state_read_returns_model_safe_view(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    memory.state.bind_task_goal(
        session_id="s1",
        task_id="task-1",
        goal_id=make_goal_id("t1"),
        source_turn_id="t1",
        evidence_text="完成安全检查",
    )
    registry = build_default_registry(memory=memory, skills=SkillStore({}))
    ctx = ToolContext(
        session_id="s1",
        turn_id="t2",
        sandbox=None,
        memory=memory,
        user_text="继续",
        observed_evidence_text="继续",
    )
    result = _run(registry.invoke("conversation_state", {"action": "read"}, ctx))
    assert "task_goal_bindings" not in result["state"]
    assert "task_goal_bindings" not in result["text"]
    # Host code still has access to the binding for completion/recovery.
    assert memory.state.goal_id_for_task("s1", "task-1") == "goal:t1"


def test_memory_limits_are_loaded_from_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ARIADNE_MEMORY_RECENT_LIMIT", "9")
    monkeypatch.setenv("ARIADNE_MEMORY_LAYER_BUDGETS", '{"semantic": 321}')
    monkeypatch.setenv("ARIADNE_MEMORY_EPISODE_MAX_EPISODES", "11")
    monkeypatch.setenv("ARIADNE_MEMORY_EPISODE_MAX_EVENTS", "12")
    monkeypatch.setenv("ARIADNE_MEMORY_CAPTURE_MAX_RECORDS", "13")
    monkeypatch.setenv("ARIADNE_MEMORY_CAPTURE_RESUME_BATCH", "14")
    settings = load_settings(workspace=tmp_path / "workspace", force_workspace=True)
    limits = settings.memory_limits
    assert limits.recent_limit == 9
    assert limits.layer_budgets["semantic"] == 321
    assert limits.episode_max_episodes == 11
    assert limits.episode_max_events_per_episode == 12
    assert limits.capture_max_records == 13
    assert limits.capture_resume_batch_size == 14


def test_memory_limits_reject_invalid_budget_json(monkeypatch) -> None:
    monkeypatch.setenv("ARIADNE_MEMORY_LAYER_BUDGETS", "not-json")
    with pytest.raises(AriadneError) as exc:
        load_settings(workspace=Path("/tmp/ariadne-memory-limit-test"), force_workspace=True)
    assert exc.value.error.code == "ARIADNE_CONFIG_INVALID"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"recent_limit": 1.5},
        {"capture_resume_batch_size": 33},
        {"episode_max_episodes": 8193},
        {"episode_max_events_per_episode": 257},
        {"capture_max_records": 16385},
        {"layer_budgets": {"semantic": 120001}},
    ],
)
def test_memory_limits_enforce_strict_types_and_hard_maxima(kwargs) -> None:
    with pytest.raises(AriadneError) as exc:
        MemoryLimits(**kwargs)
    assert exc.value.error.code == "ARIADNE_CONFIG_INVALID"


def test_partial_layer_budget_overrides_preserve_other_defaults() -> None:
    limits = MemoryLimits(layer_budgets={"semantic": 321})
    assert limits.layer_budgets["semantic"] == 321
    assert limits.layer_budgets["curated"] == 1500
    with pytest.raises(AriadneError):
        MemoryLimits(layer_budgets={"typo_layer": 10})


def test_goal_binding_read_rejects_corrupt_noncanonical_id(tmp_path: Path) -> None:
    memory = Memory.local(tmp_path / "memory")
    memory.state.bind_task_goal(
        session_id="s1",
        task_id="task-1",
        goal_id=make_goal_id("t1"),
        source_turn_id="t1",
        evidence_text="完成安全检查",
    )
    path = memory.state.path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["documents"]["s1"]["state"]["task_goal_bindings"]["task-1"] = "foo"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(AriadneError) as exc:
        memory.state.goal_id_for_task("s1", "task-1")
    assert exc.value.error.code == "ARIADNE_MEMORY_GOAL_BINDING"


def test_memory_local_applies_configured_store_limits(tmp_path: Path) -> None:
    limits = MemoryLimits(
        episode_max_episodes=7,
        episode_max_events_per_episode=8,
        capture_max_records=9,
        capture_resume_batch_size=10,
    )
    memory = Memory.local(tmp_path / "memory", limits=limits)
    assert memory.limits is limits
    assert memory.episodes.max_episodes == 7
    assert memory.episodes.max_events_per_episode == 8
    assert memory.capture_journal.max_records == 9
    assert memory.auto_capture.resume_batch_size == 10


def test_goal_id_helpers_are_canonical() -> None:
    assert make_goal_id("turn-1") == "goal:turn-1"
    assert make_goal_id("goal:turn-1") == "goal:turn-1"
    assert is_goal_id("goal:turn-1")
    assert not is_goal_id("session:current_goal")
