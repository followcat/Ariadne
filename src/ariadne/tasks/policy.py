"""Host policy for closed-loop task mode activation."""

from __future__ import annotations

from typing import Any


def resolve_task_mode(
    *,
    policy: str = "auto",
    metadata: dict[str, Any] | None = None,
    has_active_task: bool = False,
) -> tuple[bool, str]:
    """Decide whether this turn runs in closed-loop task mode.

    Precedence:
    1. Explicit metadata ``task_mode`` bool wins.
    2. ``policy=on`` always enables task mode.
    3. ``policy=off`` never enables (unless metadata forced True above).
    4. ``policy=auto`` (default): enable when an active task already exists
       for the session so resume does not require ``--task`` every turn;
       otherwise stay on the direct tool loop.

    Returns ``(enabled, reason)`` for host/traces.
    """
    meta = metadata or {}
    if "task_mode" in meta:
        flag = bool(meta.get("task_mode"))
        return flag, "metadata_task_mode" if flag else "metadata_task_mode_off"

    pol = (policy or "auto").strip().lower()
    if pol not in {"off", "on", "auto"}:
        pol = "auto"
    if pol == "on":
        return True, "policy_on"
    if pol == "off":
        return False, "policy_off"
    if has_active_task:
        return True, "active_task_resume"
    return False, "policy_auto_default_off"
