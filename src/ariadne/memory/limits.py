from __future__ import annotations

import json
import re
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

# Runtime safety ceilings.  These are intentionally independent of the
# operator-configurable defaults: a bad environment value must fail before it
# can allocate an unbounded transcript, Episode store, or journal batch.
MAX_RECENT_LIMIT = 128
MAX_LAYER_BUDGET = 120_000
MAX_EPISODES = 8_192
MAX_EVENTS_PER_EPISODE = 256
MAX_CAPTURE_RECORDS = 16_384
MAX_CAPTURE_RESUME_BATCH_SIZE = 32
_STRICT_INTEGER = re.compile(r"^[+-]?\d+$")


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
        limits = {
            "recent_limit": (MAX_RECENT_LIMIT, "recent raw message limit"),
            "episode_max_episodes": (MAX_EPISODES, "Episode capacity"),
            "episode_max_events_per_episode": (
                MAX_EVENTS_PER_EPISODE,
                "events per Episode capacity",
            ),
            "capture_max_records": (MAX_CAPTURE_RECORDS, "capture journal capacity"),
            "capture_resume_batch_size": (
                MAX_CAPTURE_RESUME_BATCH_SIZE,
                "capture resume batch size",
            ),
        }
        for name, (maximum, label) in limits.items():
            raw = getattr(self, name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory limit {name} must be a strict integer",
                        field=name,
                        value=raw,
                    )
                )
            value = raw
            if value < 1 or value > maximum:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"{label} must be in range 1..{maximum}",
                        field=name,
                        value=value,
                    )
                )
            setattr(self, name, value)
        if not isinstance(self.layer_budgets, dict):
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "memory layer budgets must be an object",
                    field="layer_budgets",
                    value=self.layer_budgets,
                )
            )
        unknown = set(self.layer_budgets) - set(DEFAULT_LAYER_BUDGETS)
        if unknown:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "unknown memory layer budget key",
                    field=sorted(str(key) for key in unknown),
                )
            )
        # Partial configuration is an override, never an opt-out from the
        # remaining layer safety budgets.
        normalized: dict[str, int] = dict(DEFAULT_LAYER_BUDGETS)
        for key, value in dict(self.layer_budgets).items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        "memory layer budgets must be strict integers",
                        field=str(key),
                        value=value,
                    )
                )
            budget = value
            if budget < 1 or budget > MAX_LAYER_BUDGET:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory layer budget must be in range 1..{MAX_LAYER_BUDGET}",
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
            if isinstance(raw, bool) or isinstance(raw, float):
                parsed: int | None = None
            elif isinstance(raw, int):
                parsed = raw
            elif isinstance(raw, str) and _STRICT_INTEGER.fullmatch(raw.strip()):
                parsed = int(raw.strip())
            else:
                parsed = None
            if parsed is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_CONFIG_INVALID",
                        f"memory limit {keys[0]} must be a strict integer",
                        field=keys[0],
                        value=raw,
                    )
                )
            return parsed

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
            budgets = dict(DEFAULT_LAYER_BUDGETS)
            for key, value in decoded.items():
                if isinstance(value, bool) or not isinstance(value, int):
                    raise AriadneError(
                        app_error(
                            "ARIADNE_CONFIG_INVALID",
                            "ARIADNE_MEMORY_LAYER_BUDGETS values must be strict integers",
                            field=str(key),
                            value=value,
                        )
                    )
                budgets[str(key)] = value
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
