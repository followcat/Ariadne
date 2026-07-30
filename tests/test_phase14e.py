from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.delegation import ControlledDelegator
from ariadne.model.base import ModelExchange
from ariadne.tasks import (
    Check,
    DeterministicVerifier,
    ScheduledGoalStore,
    SemanticVerifier,
    SQLiteTaskStore,
    TaskController,
)
from ariadne.types import Message, ToolCallTrace, Usage


class StructuredModel:
    model = "fake"

    def __init__(self, *, quote: str = "good") -> None:
        self.quote = quote

    async def complete(self, **kwargs):
        tool_name = kwargs["tools"][0]["function"]["name"]
        if tool_name == "report_semantic_check":
            arguments = {
                "status": "pass",
                "evidence_quote": self.quote,
                "rationale": "the quoted result satisfies the qualitative criterion",
            }
        else:
            arguments = {
                "conclusion": "the evidence supports this subgoal",
                "evidence_quote": self.quote,
                "confidence": 0.8,
            }
        return ModelExchange(
            message=Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "structured",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(arguments),
                        },
                    }
                ],
            ),
            usage=Usage(),
            raw={},
        )


def test_semantic_verifier_is_evidence_quoting_and_completes_task(tmp_path: Path) -> None:
    controller = TaskController(
        store=SQLiteTaskStore(tmp_path / "tasks.sqlite3"),
        verifier=DeterministicVerifier(tmp_path),
        semantic_verifier=SemanticVerifier(StructuredModel()),
    )
    state = controller.create_from_plan(
        session_id="s1",
        user_id="u1",
        original_user_goal="qualitative review",
        arguments={
            "goal": "qualitative review",
            "steps": [
                {
                    "intent": "review output quality",
                    "done_when": [
                        {
                            "kind": "llm_semantic",
                            "spec": {
                                "criterion": "the review says quality is good",
                                "oracle_unavailable_reason": "quality is qualitative",
                            },
                        }
                    ],
                }
            ],
            "goal_checks": [
                {
                    "kind": "llm_semantic",
                    "spec": {
                        "criterion": "the review says quality is good",
                        "oracle_unavailable_reason": "quality is qualitative",
                    },
                }
            ],
        },
    )
    state, _step, attempt_id = controller.start_attempt(state)
    trace = ToolCallTrace(
        call_id="c1",
        name="review_tool",
        arguments={},
        output={"quality": "good"},
        status="completed",
    )
    outcome = asyncio.run(
        controller.record_attempt_async(
            state,
            traces=[trace],
            spec=None,
            effect_level="read",
            attempt_id=attempt_id,
        )
    )
    assert outcome.state.status == "completed"
    result = outcome.step.check_results[0]
    assert result.status == "pass"
    assert result.observed_value["evidence_quote"] == "good"


def test_semantic_check_rejected_when_deterministic_oracle_is_present(tmp_path: Path) -> None:
    controller = TaskController(
        store=SQLiteTaskStore(tmp_path / "tasks.sqlite3"),
        verifier=DeterministicVerifier(tmp_path),
        semantic_verifier=SemanticVerifier(StructuredModel()),
    )
    with pytest.raises(AriadneError) as caught:
        controller.create_from_plan(
            session_id="s1",
            user_id="u1",
            original_user_goal="mixed oracle",
            arguments={
                "goal": "mixed oracle",
                "steps": [
                    {
                        "intent": "verify",
                        "done_when": [
                            {"kind": "path_exists", "spec": {"path": "done.txt"}},
                            {
                                "kind": "llm_semantic",
                                "spec": {
                                    "criterion": "looks done",
                                    "oracle_unavailable_reason": "claimed qualitative",
                                },
                            },
                        ],
                    }
                ],
                "goal_checks": [
                    {"kind": "path_exists", "spec": {"path": "done.txt"}}
                ],
            },
        )
    assert caught.value.error.code == "ARIADNE_TASK_INVALID"
    assert "no deterministic check" in caught.value.error.message


def test_multimodal_image_file_check_verifies_bytes_dimensions_and_digest(
    tmp_path: Path,
) -> None:
    # Enough PNG structure for deterministic signature + IHDR dimensions parsing.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (3).to_bytes(4, "big") + (2).to_bytes(4, "big")
    (tmp_path / "plot.png").write_bytes(png)
    check = Check.from_plan(
        {
            "kind": "image_file",
            "spec": {
                "path": "plot.png",
                "format": "png",
                "min_width": 3,
                "min_height": 2,
                "min_bytes": 20,
            },
        }
    )
    result = DeterministicVerifier(tmp_path).run(
        check, traces=[], attempt_id="image-attempt"
    )
    assert result.status == "pass"
    assert result.observed_value["width"] == 3
    assert result.observed_value["height"] == 2
    assert len(result.observed_value["sha256"]) == 64
    assert result.evidence[0].kind == "image"


def test_host_scheduled_goal_claims_checks_and_emits_notifications(tmp_path: Path) -> None:
    scheduler = ScheduledGoalStore(tmp_path / "scheduled.sqlite3")
    created = scheduler.create(
        user_id="alice",
        session_id="s1",
        goal="wait for export",
        check={"kind": "path_exists", "spec": {"path": "export.csv"}},
        interval_seconds=60,
        next_run_at=1000,
    )
    first = scheduler.run_due(
        user_id="alice",
        worker_id="cron-1",
        verifier=DeterministicVerifier(tmp_path),
        now=1000,
    )
    assert first[0]["status"] == "active"
    assert first[0]["notification_kind"] == "goal_pending"
    assert scheduler.claim_due(user_id="alice", worker_id="cron-2", now=1001) is None

    (tmp_path / "export.csv").write_text("done", encoding="utf-8")
    second = scheduler.run_due(
        user_id="alice",
        worker_id="cron-2",
        verifier=DeterministicVerifier(tmp_path),
        now=1060,
    )
    assert second[0]["status"] == "completed"
    assert scheduler.get(created["schedule_id"], user_id="alice")["status"] == "completed"
    notifications = scheduler.notifications(user_id="alice")
    assert [item["kind"] for item in notifications] == ["goal_pending", "goal_satisfied"]


def test_scheduled_goal_stale_worker_cannot_complete_reclaimed_lease(
    tmp_path: Path,
) -> None:
    scheduler = ScheduledGoalStore(tmp_path / "scheduled.sqlite3")
    created = scheduler.create(
        user_id="alice",
        session_id="s1",
        goal="observe export",
        check={"kind": "path_exists", "spec": {"path": "export.csv"}},
        interval_seconds=60,
        next_run_at=1000,
    )
    (tmp_path / "export.csv").write_text("done", encoding="utf-8")

    class ReclaimingVerifier:
        def run(self, check, *, traces, attempt_id):
            reclaimed = scheduler.claim_due(
                user_id="alice",
                worker_id="worker-new",
                now=1031,
            )
            assert reclaimed is not None
            assert reclaimed["lease_token"] == 2
            return DeterministicVerifier(tmp_path).run(
                check,
                traces=traces,
                attempt_id=attempt_id,
            )

    with pytest.raises(AriadneError) as caught:
        scheduler.run_due(
            user_id="alice",
            worker_id="worker-old",
            verifier=ReclaimingVerifier(),  # type: ignore[arg-type]
            now=1000,
        )
    assert caught.value.error.code == "ARIADNE_SCHEDULE_CONFLICT"
    current = scheduler.get(created["schedule_id"], user_id="alice")
    assert current["lease_owner"] == "worker-new"
    assert current["lease_token"] == 2
    assert scheduler.notifications(user_id="alice") == []


def test_controlled_delegation_is_bounded_grounded_and_toolless() -> None:
    reports = asyncio.run(
        ControlledDelegator(StructuredModel()).run(
            subgoals=["analyze risk", "analyze usability"],
            evidence_text="the observed quality is good",
        )
    )
    assert len(reports) == 2
    assert all(report["verified"] for report in reports)
    assert all(report["capabilities_exposed"] == 0 for report in reports)

    with pytest.raises(AriadneError) as caught:
        asyncio.run(
            ControlledDelegator(StructuredModel(quote="invented")).run(
                subgoals=["analyze risk", "analyze usability"],
                evidence_text="only grounded evidence",
            )
        )
    assert caught.value.error.code == "ARIADNE_DELEGATION_PROTOCOL"
