from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.skills.outcomes import SkillOutcomeLedger
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolContext, build_default_registry
from ariadne.types import SkillEvent


def _record(
    ledger: SkillOutcomeLedger,
    *,
    index: int,
    adopted: bool,
    outcome: str = "completed",
) -> None:
    ledger.record_turn(
        turn_id=f"t{index}",
        session_id="s1",
        candidates=[("planner", 0.8)],
        loaded={"planner"},
        adopted={"planner"} if adopted else set(),
        tool_names=["sandbox_exec"],
        turn_outcome=outcome,
        at=1_000_000 + index,
    )


def test_loaded_success_is_not_treated_as_skill_causality(tmp_path: Path) -> None:
    ledger = SkillOutcomeLedger(tmp_path / "outcomes.json", min_samples=5)
    for index in range(5):
        _record(ledger, index=index, adopted=False)
    evidence = ledger.adjustment("planner", now=1_000_100)
    assert evidence.positive == 0
    assert evidence.false_loads == 5
    assert evidence.adjustment < 0


def test_explicit_adoption_outcomes_adjust_ranking_and_can_be_disabled(tmp_path: Path) -> None:
    ledger = SkillOutcomeLedger(tmp_path / "outcomes.json", min_samples=5)
    for index in range(5):
        _record(ledger, index=index, adopted=True)
    evidence = ledger.adjustment("planner", now=1_000_100)
    assert evidence.positive == 5
    assert evidence.adjustment > 0
    assert "half_life_days" in evidence.reason

    ledger.set_ranking_enabled(False)
    disabled = ledger.adjustment("planner", now=1_000_100)
    assert disabled.adjustment == 0
    assert disabled.enabled is False


def test_scoped_skill_outcome_does_not_credit_an_unbound_adoption(
    tmp_path: Path,
) -> None:
    ledger = SkillOutcomeLedger(tmp_path / "outcomes.json", min_samples=1)
    ledger.record_turn(
        turn_id="t1",
        session_id="s1",
        candidates=[("planner", 0.8)],
        loaded={"planner"},
        adopted={"planner"},
        tool_names=["sandbox_write_file"],
        turn_outcome="completed",
        step_outcome="verified",
        task_outcome="completed",
        task_id="task-global",
        step_id="step-global",
        attempt_id="attempt-global",
        skill_attributions={},
        at=1_000_000,
    )

    event = ledger.list_events(skill_name="planner")[0]
    assert event["attempt_attributed"] is False
    assert event["tool_names_used"] == []
    assert event["step_outcome"] == ""
    assert event["task_outcome"] == ""
    assert event["task_id"] == ""
    assert ledger.adjustment("planner", now=1_000_001).negative == 1


def test_skill_patch_requires_evidence_and_host_confirmation(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    store = SkillStore.from_dir(user_root, strict=False, user_root=user_root, namespace="user")
    store.manage(
        action="create",
        name="planner",
        description="plan tasks",
        body="old body",
    )
    with pytest.raises(AriadneError) as caught:
        store.manage(
            action="update",
            name="planner",
            description="new",
            body="new body",
        )
    assert caught.value.error.code == "ARIADNE_SKILL_CONFIRMATION_REQUIRED"

    proposal = store.patches().propose(
        name="planner",
        description="plan verified tasks",
        body="new body",
        keywords=["verify"],
        evidence=["turn t9 failed because the old steps had no oracle"],
        expected_version="1",
    )
    assert proposal["status"] == "pending"
    assert "@@" in proposal["diff"]
    assert store.get("planner").body.strip() == "old body"

    applied = store.patches().confirm(
        proposal_id=proposal["proposal_id"], confirmed_by="alice"
    )
    assert applied["status"] == "applied"
    assert applied["version"] == "2"
    assert Path(applied["snapshot"]).is_dir()
    assert store.get("planner").body.strip() == "new body"
    assert store.patches().get(proposal["proposal_id"])["confirmed_by"] == "alice"


def test_adopt_skill_requires_a_real_loaded_body(tmp_path: Path) -> None:
    user_root = tmp_path / "user"
    user_root.mkdir()
    store = SkillStore.from_dir(user_root, strict=False, user_root=user_root, namespace="user")
    store.manage(action="create", name="planner", description="plan", body="steps")
    registry = build_default_registry(skills=store, enable_deferred_demo=False)
    events: list[SkillEvent] = []
    context = ToolContext(
        session_id="s1",
        turn_id="t1",
        sandbox=None,
        skills=store,
        skill_events=events,
    )
    with pytest.raises(AriadneError) as caught:
        asyncio.run(
            registry.invoke(
                "adopt_skill", {"name": "planner", "reason": "using its steps"}, context
            )
        )
    assert caught.value.error.code == "ARIADNE_SKILL_ADOPTION_INVALID"

    events.append(SkillEvent(kind="load", skill_name="planner"))
    result = asyncio.run(
        registry.invoke(
            "adopt_skill", {"name": "planner", "reason": "using its steps"}, context
        )
    )
    assert result["adopted"] is True
    assert any(event.kind == "adopt" for event in events)
