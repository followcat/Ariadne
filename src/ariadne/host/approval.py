from __future__ import annotations

from typing import Any

from ..errors import AriadneError, app_error
from ..tools.registry import ApprovalHook


def make_noninteractive_approval_hook(mode: str) -> ApprovalHook | None:
    """Build the safe host policy used when no approval UI is attached.

    Interactive hosts may replace this hook with their own approval workflow.
    A non-interactive host cannot satisfy ``on-request`` approvals, so material
    effects are denied instead of being executed without authorization.
    """

    normalized = str(mode or "").strip().lower()
    if normalized == "auto":
        return None
    if normalized not in {"on-request", "readonly"}:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown approval mode: {mode!r}",
            )
        )

    def hook(
        _name: str,
        _arguments: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        effect = str(metadata.get("side_effect_level") or "unknown")
        return effect in {"none", "read"}

    return hook
