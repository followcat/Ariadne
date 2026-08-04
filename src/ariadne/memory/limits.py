from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from ..errors import AriadneError, app_error

DEFAULT_LAYER_BUDGETS = {
    "conversation_state": 2500,
    "curated": 1500,
    "turn_summary": 2000,
    "semantic": 1500,
    "user_model": 2000,
    "reflection": 1200,
    "prospective": 1200,
}


@dataclass(slots=True)
class MemoryLimits:
    """Host-configurable runtime budgets for the layered memory stack."""

    recent_limit: int = 4
    layer_budgets: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_LAYER_BUDGETS)
    )
    episode_max_episodes: int = 1024
    episode_max_events_per_episode: int = 256
    capture_max_records: int = 4096
    capture_resume_batch_size: int = 4

    def __post_init__(self) -> None:
        for name in (
            "recent_limit",
            "episode_max_episodes",
            "episode_max_events_per_episode",
            "capture_max_records",
            "capture_resume_batch_size",
        ):
            raw = getattr(self, name)
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory limit {name} must be an integer",
                        field=name,
                        value=raw,
                    )
                ) from exc
            if value < 1:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory limit {name} must be positive",
                        field=name,
                        value=value,
                    )
                )
            setattr(self, name, value)
        normalized: dict[str, int] = {}
        for key, value in dict(self.layer_budgets).items():
            try:
                budget = int(value)
            except (TypeError, ValueError) as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "memory layer budgets must be integers",
                        field=str(key),
                        value=value,
                    )
                ) from exc
            if budget < 1:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "memory layer budgets must be positive",
                        field=str(key),
                        value=budget,
                    )
                )
            normalized[str(key)] = budget
        self.layer_budgets = normalized

    @classmethod
    def from_env(cls, pick: Callable[..., str]) -> "MemoryLimits":
        def integer(*keys: str, default: int) -> int:
            raw = pick(*keys, default=str(default))
            try:
                return int(raw)
            except (TypeError, ValueError) as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory limit {keys[0]} must be an integer",
                        field=keys[0],
                        value=raw,
                    )
                ) from exc

        budgets = dict(DEFAULT_LAYER_BUDGETS)
        raw_budgets = pick("ARIADNE_MEMORY_LAYER_BUDGETS", default="").strip()
        if raw_budgets:
            try:
                decoded = json.loads(raw_budgets)
            except json.JSONDecodeError as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "ARIADNE_MEMORY_LAYER_BUDGETS must be a JSON object",
                    )
                ) from exc
            if not isinstance(decoded, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "ARIADNE_MEMORY_LAYER_BUDGETS must be a JSON object",
                    )
                )
            try:
                budgets = {str(key): int(value) for key, value in decoded.items()}
            except (TypeError, ValueError) as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "ARIADNE_MEMORY_LAYER_BUDGETS values must be integers",
                    )
                ) from exc
        return cls(
            recent_limit=integer("ARIADNE_MEMORY_RECENT_LIMIT", default=4),
            layer_budgets=budgets,
            episode_max_episodes=integer(
                "ARIADNE_MEMORY_EPISODE_MAX_EPISODES", default=1024
            ),
            episode_max_events_per_episode=integer(
                "ARIADNE_MEMORY_EPISODE_MAX_EVENTS_PER_EPISODE",
                "ARIADNE_MEMORY_EPISODE_MAX_EVENTS",
                default=256,
            ),
            capture_max_records=integer(
                "ARIADNE_MEMORY_CAPTURE_MAX_RECORDS", default=4096
            ),
            capture_resume_batch_size=integer(
                "ARIADNE_MEMORY_CAPTURE_RESUME_BATCH_SIZE",
                "ARIADNE_MEMORY_CAPTURE_RESUME_BATCH",
                default=4,
            ),
        )
