from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from ..memory.json_file import locked_read_json, locked_update_json, locked_write_json


@dataclass(slots=True)
class RankingAdjustment:
    skill_name: str
    adjustment: float
    sample_count: int
    effective_weight: float
    positive: int
    negative: int
    false_loads: int
    enabled: bool
    reason: str


@dataclass(slots=True)
class SkillOutcomeLedger:
    """Append-only use→outcome evidence for explainable skill ranking."""

    path: Path
    min_samples: int = 5
    half_life_days: float = 30.0
    max_adjustment: float = 0.15
    max_events: int = 20_000

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.min_samples < 1 or self.half_life_days <= 0 or self.max_adjustment <= 0:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "invalid skill outcome ranking policy")
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(
                self.path,
                {"schema_version": 1, "ranking_enabled": True, "events": []},
            )

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(
            self.path,
            default={"schema_version": 1, "ranking_enabled": True, "events": []},
        )
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_SKILL_OUTCOME_INVALID", "unknown skill outcome schema")
            )
        return data

    def ranking_enabled(self) -> bool:
        return bool(self._read().get("ranking_enabled", True))

    def set_ranking_enabled(self, enabled: bool) -> dict[str, Any]:
        def mut(data: dict[str, Any]) -> dict[str, Any]:
            data["schema_version"] = 1
            data["ranking_enabled"] = bool(enabled)
            data.setdefault("events", [])
            return data

        updated = locked_update_json(self.path, mut, default={"schema_version": 1, "events": []})
        return {"ranking_enabled": bool(updated["ranking_enabled"])}

    def record_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        candidates: list[tuple[str, float]],
        loaded: set[str],
        adopted: set[str],
        tool_names: list[str],
        turn_outcome: str,
        step_outcome: str = "",
        task_outcome: str = "",
        task_id: str = "",
        step_id: str = "",
        attempt_id: str = "",
        skill_attributions: dict[str, dict[str, Any]] | None = None,
        user_corrected: bool = False,
        at: float | None = None,
    ) -> list[str]:
        if adopted - loaded:
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_OUTCOME_INVALID",
                    "a skill must be loaded before it can be explicitly adopted",
                    adopted=sorted(adopted),
                    loaded=sorted(loaded),
                )
            )
        score_by_name = {str(name): float(score) for name, score in candidates}
        names = list(dict.fromkeys([*score_by_name, *sorted(loaded)]))
        now = time.time() if at is None else float(at)
        scoped_attribution = skill_attributions is not None
        attributions = skill_attributions if skill_attributions is not None else {}
        rows: list[dict[str, Any]] = []
        for name in names:
            attribution = attributions.get(name)
            rows.append(
                {
                    "event_id": uuid.uuid4().hex[:16],
                    "kind": "turn_outcome",
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "skill_name": name,
                    "candidate_score": score_by_name.get(name, 0.0),
                    "loaded": name in loaded,
                    "adopted": name in adopted,
                    "attribution_scoped": scoped_attribution,
                    "attempt_attributed": attribution is not None,
                    "tool_names_used": list(
                        dict.fromkeys(
                            list(attribution.get("tool_names") or [])
                            if attribution is not None
                            else ([] if scoped_attribution else tool_names)
                        )
                    ),
                    "turn_outcome": turn_outcome,
                    "step_outcome": (
                        str(attribution.get("step_outcome") or "")
                        if attribution is not None
                        else ("" if scoped_attribution else step_outcome)
                    ),
                    "task_outcome": (
                        str(attribution.get("task_outcome") or "")
                        if attribution is not None
                        else ("" if scoped_attribution else task_outcome)
                    ),
                    "task_id": (
                        str(attribution.get("task_id") or "")
                        if attribution is not None
                        else ("" if scoped_attribution else task_id)
                    ),
                    "step_id": (
                        str(attribution.get("step_id") or "")
                        if attribution is not None
                        else ("" if scoped_attribution else step_id)
                    ),
                    "attempt_id": (
                        str(attribution.get("attempt_id") or "")
                        if attribution is not None
                        else ("" if scoped_attribution else attempt_id)
                    ),
                    "user_corrected": bool(user_corrected),
                    "at": now,
                }
            )

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            events = data.setdefault("events", [])
            if len(events) + len(rows) > self.max_events:
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_OUTCOME_CAPACITY",
                        "skill outcome ledger capacity exceeded; export/archive explicitly",
                        max_events=self.max_events,
                    )
                )
            events.extend(rows)
            data["schema_version"] = 1
            data.setdefault("ranking_enabled", True)
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "events": []})
        return [str(row["event_id"]) for row in rows]

    def record_user_correction(
        self,
        *,
        turn_id: str,
        skill_name: str,
        reason: str,
        at: float | None = None,
    ) -> str:
        if not turn_id.strip() or not skill_name.strip() or not reason.strip():
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_OUTCOME_INVALID",
                    "turn_id, skill_name, and correction reason are required",
                )
            )
        event_id = uuid.uuid4().hex[:16]
        row = {
            "event_id": event_id,
            "kind": "user_correction",
            "turn_id": turn_id,
            "skill_name": skill_name,
            "reason": reason,
            "user_corrected": True,
            "at": time.time() if at is None else float(at),
        }

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            events = data.setdefault("events", [])
            if len(events) >= self.max_events:
                raise AriadneError(
                    app_error("ARIADNE_SKILL_OUTCOME_CAPACITY", "skill outcome ledger is full")
                )
            events.append(row)
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "events": []})
        return event_id

    def list_events(
        self, *, skill_name: str | None = None, turn_id: str | None = None
    ) -> list[dict[str, Any]]:
        events = list(self._read().get("events") or [])
        if skill_name is not None:
            events = [row for row in events if row.get("skill_name") == skill_name]
        if turn_id is not None:
            events = [row for row in events if row.get("turn_id") == turn_id]
        return events

    def adjustment(self, skill_name: str, *, now: float | None = None) -> RankingAdjustment:
        data = self._read()
        enabled = bool(data.get("ranking_enabled", True))
        rows = [
            row
            for row in data.get("events") or []
            if row.get("skill_name") == skill_name
            and (row.get("loaded") or row.get("kind") == "user_correction")
        ]
        sample_count = len(rows)
        if not enabled:
            return RankingAdjustment(
                skill_name, 0.0, sample_count, 0.0, 0, 0, 0, False, "ranking disabled"
            )
        if sample_count < self.min_samples:
            return RankingAdjustment(
                skill_name,
                0.0,
                sample_count,
                0.0,
                0,
                0,
                sum(1 for row in rows if row.get("loaded") and not row.get("adopted")),
                True,
                f"minimum sample gate {sample_count}/{self.min_samples}",
            )

        clock = time.time() if now is None else float(now)
        weighted_sum = 0.0
        total_weight = 0.0
        positive = 0
        negative = 0
        false_loads = 0
        for row in rows:
            age_days = max(0.0, (clock - float(row.get("at") or clock)) / 86_400)
            weight = math.exp(-math.log(2) * age_days / self.half_life_days)
            if row.get("kind") == "user_correction" or row.get("user_corrected"):
                signal = -1.0
                negative += 1
            elif row.get("adopted"):
                success = bool(
                    not row.get("attribution_scoped")
                    or row.get("attempt_attributed")
                ) and (
                    row.get("step_outcome") == "verified"
                    or row.get("task_outcome") == "completed"
                    or (
                        not row.get("step_outcome")
                        and not row.get("task_outcome")
                        and row.get("turn_outcome") == "completed"
                    )
                )
                signal = 1.0 if success else -1.0
                positive += int(success)
                negative += int(not success)
            else:
                signal = -0.2
                false_loads += 1
            weighted_sum += signal * weight
            total_weight += weight
        mean = weighted_sum / total_weight if total_weight else 0.0
        confidence = min(1.0, sample_count / (self.min_samples * 2))
        adjustment = max(
            -self.max_adjustment,
            min(self.max_adjustment, mean * self.max_adjustment * confidence),
        )
        return RankingAdjustment(
            skill_name=skill_name,
            adjustment=adjustment,
            sample_count=sample_count,
            effective_weight=total_weight,
            positive=positive,
            negative=negative,
            false_loads=false_loads,
            enabled=True,
            reason=(
                f"decayed evidence: +{positive}/-{negative}, false_loads={false_loads}, "
                f"half_life_days={self.half_life_days:g}"
            ),
        )
