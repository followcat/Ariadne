from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

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

# Profiles are the personal-kernel auto path. Individual env keys remain
# optional overrides for operators who need a single knob.
MEMORY_PROFILES = frozenset({"compact", "default", "deep"})
# Layer budgets in DEFAULT_LAYER_BUDGETS are calibrated for this context size.
REFERENCE_CONTEXT_CHARS = 120_000

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


def validate_capacity(value: Any, *, field: str, maximum: int) -> int:
    """Validate a public memory capacity without coercing its type."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"memory limit {field} must be a strict integer",
                field=field,
                value=value,
            )
        )
    if value < 1 or value > maximum:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"memory limit {field} must be in range 1..{maximum}",
                field=field,
                value=value,
            )
        )
    return value


def _scale_int(value: int, factor: float, *, minimum: int, maximum: int) -> int:
    scaled = int(math.floor(value * factor + 0.5))
    return max(minimum, min(maximum, scaled))


@dataclass(slots=True)
class MemoryLimits:
    """Host-configurable runtime budgets for the layered memory stack.

    Personal default path is automatic: use :meth:`for_profile` (or bare
    construction for the ``default`` profile). Operators may override single
    fields via env; hard ceilings always apply and never silent-clamp bad
    values past the maximum (they fail validation instead).
    """

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
            "recent_limit": MAX_RECENT_LIMIT,
            "episode_max_episodes": MAX_EPISODES,
            "episode_max_events_per_episode": MAX_EVENTS_PER_EPISODE,
            "capture_max_records": MAX_CAPTURE_RECORDS,
            "capture_resume_batch_size": MAX_CAPTURE_RESUME_BATCH_SIZE,
        }
        for name, maximum in limits.items():
            raw = getattr(self, name)
            setattr(self, name, validate_capacity(raw, field=name, maximum=maximum))
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
    def for_profile(cls, profile: str = "default") -> "MemoryLimits":
        """Build limits for a named personal profile (zero-config path)."""

        name = (profile or "default").strip().lower()
        if name not in MEMORY_PROFILES:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    f"unknown memory profile: {profile!r} "
                    f"(compact|default|deep)",
                    profile=profile,
                )
            )
        if name == "compact":
            return cls(
                recent_limit=2,
                layer_budgets={
                    key: max(200, int(value * 0.6))
                    for key, value in DEFAULT_LAYER_BUDGETS.items()
                },
                episode_max_episodes=512,
                episode_max_events_per_episode=128,
                capture_max_records=2048,
                capture_resume_batch_size=2,
            )
        if name == "deep":
            return cls(
                recent_limit=8,
                layer_budgets={
                    key: min(MAX_LAYER_BUDGET, max(200, int(value * 1.5)))
                    for key, value in DEFAULT_LAYER_BUDGETS.items()
                },
                episode_max_episodes=2048,
                episode_max_events_per_episode=256,
                capture_max_records=8192,
                capture_resume_batch_size=8,
            )
        return cls()

    def scaled_to_context(self, context_max_chars: int) -> "MemoryLimits":
        """Return a copy with recent/layer budgets scaled to a context window.

        Store capacities (episodes, journal records) stay as profile/operator
        safety ceilings — only prompt-adjacent budgets track context size.
        Factor is clamped so tiny/huge windows do not collapse or explode
        layers; values still pass hard maxima via construction.
        """

        if isinstance(context_max_chars, bool) or not isinstance(
            context_max_chars, int
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "context_max_chars must be a strict integer",
                    field="context_max_chars",
                    value=context_max_chars,
                )
            )
        if context_max_chars < 4_000 or context_max_chars > 2_000_000:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "context_max_chars must be in range 4000..2000000",
                    field="context_max_chars",
                    value=context_max_chars,
                )
            )
        factor = context_max_chars / float(REFERENCE_CONTEXT_CHARS)
        factor = max(0.5, min(2.0, factor))
        budgets = {
            key: _scale_int(
                value, factor, minimum=200, maximum=MAX_LAYER_BUDGET
            )
            for key, value in self.layer_budgets.items()
        }
        recent = _scale_int(
            self.recent_limit, factor, minimum=1, maximum=MAX_RECENT_LIMIT
        )
        return MemoryLimits(
            recent_limit=recent,
            layer_budgets=budgets,
            episode_max_episodes=self.episode_max_episodes,
            episode_max_events_per_episode=self.episode_max_events_per_episode,
            capture_max_records=self.capture_max_records,
            capture_resume_batch_size=self.capture_resume_batch_size,
        )

    @classmethod
    def from_env(
        cls,
        pick: Callable[..., str],
        *,
        context_max_chars: int | None = None,
    ) -> "MemoryLimits":
        """Load limits: profile auto-defaults, then optional field overrides.

        Order:
        1. ``ARIADNE_MEMORY_PROFILE`` (default ``default``) → :meth:`for_profile`
        2. Per-field ``ARIADNE_MEMORY_*`` overrides when the env key is set
        3. Optional ``ARIADNE_MEMORY_SCALE_TO_CONTEXT=1`` scales recent/layers
           from ``context_max_chars`` (host context budget)
        """

        def optional_integer(*keys: str) -> int | None:
            for key in keys:
                raw = pick(key, default="")
                if not str(raw).strip():
                    continue
                text = str(raw).strip()
                if not _STRICT_INTEGER.fullmatch(text):
                    raise AriadneError(
                        app_error(
                            "ARIADNE_CONFIG_INVALID",
                            f"memory limit {key} must be a strict integer",
                            field=key,
                            value=raw,
                        )
                    )
                return int(text)
            return None

        profile = (
            pick("ARIADNE_MEMORY_PROFILE", default="default") or "default"
        ).strip().lower()
        base = cls.for_profile(profile)

        recent = optional_integer("ARIADNE_MEMORY_RECENT_LIMIT")
        episodes = optional_integer("ARIADNE_MEMORY_EPISODE_MAX_EPISODES")
        events = optional_integer(
            "ARIADNE_MEMORY_EPISODE_MAX_EVENTS_PER_EPISODE",
            "ARIADNE_MEMORY_EPISODE_MAX_EVENTS",
        )
        records = optional_integer("ARIADNE_MEMORY_CAPTURE_MAX_RECORDS")
        batch = optional_integer(
            "ARIADNE_MEMORY_CAPTURE_RESUME_BATCH_SIZE",
            "ARIADNE_MEMORY_CAPTURE_RESUME_BATCH",
        )

        budgets = dict(base.layer_budgets)
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

        limits = cls(
            recent_limit=recent if recent is not None else base.recent_limit,
            layer_budgets=budgets,
            episode_max_episodes=(
                episodes if episodes is not None else base.episode_max_episodes
            ),
            episode_max_events_per_episode=(
                events
                if events is not None
                else base.episode_max_events_per_episode
            ),
            capture_max_records=(
                records if records is not None else base.capture_max_records
            ),
            capture_resume_batch_size=(
                batch if batch is not None else base.capture_resume_batch_size
            ),
        )

        scale_raw = pick("ARIADNE_MEMORY_SCALE_TO_CONTEXT", default="0")
        scale_on = str(scale_raw).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if scale_on:
            ctx = (
                context_max_chars
                if context_max_chars is not None
                else REFERENCE_CONTEXT_CHARS
            )
            limits = limits.scaled_to_context(ctx)
        return limits
