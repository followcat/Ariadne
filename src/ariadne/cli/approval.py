"""Host-side approval policies for tool invocations.

Modes (cli-shell-agent approval section):
- auto:        everything allowed (default, previous behavior)
- on-request:  write-class tools ask the user (rich Confirm), reads pass
- readonly:    write-class tools are always denied

When a :class:`~ariadne.cli.grants.GrantStore` is provided, on-request decisions
are persisted (pending → approved/denied/executed/expired) so restarts keep
prior approvals for matching tool fingerprints within TTL.
"""

from __future__ import annotations

from typing import Any, Callable

from . import ui
from .grants import GrantStore

# Legacy display/export list; policy decisions use ToolSpec effect metadata.
WRITE_TOOLS = {"sandbox_exec", "sandbox_write_file", "sandbox_edit_file", "skill_manage"}

ConfirmFn = Callable[[str], bool]


def _describe(name: str, args: dict[str, Any]) -> str:
    if name == "sandbox_exec":
        return f"$ {args.get('cmd') or ''}"
    if name in {"sandbox_write_file", "sandbox_edit_file"}:
        return str(args.get("path") or "")
    if name == "skill_manage":
        return f"{args.get('action') or ''} {args.get('name') or ''}"
    return name


def make_approval_hook(
    mode: str,
    *,
    confirm: ConfirmFn | None = None,
    grant_store: GrantStore | None = None,
    session_id: str = "",
) -> Callable[[str, dict[str, Any], dict[str, Any]], bool] | None:
    """Build the approval hook for a mode. confirm defaults to rich prompt."""
    if mode == "auto":
        return None
    if mode not in {"on-request", "readonly"}:
        from ..errors import AriadneError, app_error

        raise AriadneError(
            app_error("ARIADNE_CONFIG_INVALID", f"unknown approval mode: {mode!r}")
        )

    if confirm is None:
        from rich.prompt import Confirm

        def confirm(question: str) -> bool:  # type: ignore[no-redef]
            try:
                return Confirm.ask(question, default=False, console=ui.console)
            except EOFError:
                # no input available (pipe closed): safe default is deny
                ui.print_info("no input available — denied by default")
                return False

    def hook(
        name: str,
        args: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        effect = str((metadata or {}).get("side_effect_level") or "unknown")
        if effect in {"none", "read"}:
            return True
        if mode == "readonly":
            ui.print_info(
                f"denied (readonly, effect={effect}): {name} {_describe(name, args)}"
            )
            return False

        # Reuse durable grant for same fingerprint (approved or executed, not expired)
        if grant_store is not None:
            grant_store.expire_due()
            existing = grant_store.find_usable(name, args or {})
            if existing is not None:
                # Keep status as approved/executed for TTL reuse across restarts;
                # mark executed when first allowed so audit shows tool ran.
                if existing.get("status") == "approved":
                    grant_store.mark_executed(str(existing["id"]))
                ui.print_info(f"approved (grant {str(existing['id'])[:8]}…): {name}")
                return True

        ui.console.print(
            f"[yellow]approval requested[/] [bold]{name}[/] "
            f"effect={effect} {_describe(name, args)}"
        )
        grant: dict[str, Any] | None = None
        if grant_store is not None:
            grant = grant_store.create_pending(
                name=name, args=args or {}, session_id=session_id
            )
        allowed = bool(confirm("Allow?"))
        if grant_store is not None and grant is not None:
            gid = str(grant["id"])
            if allowed:
                # Leave status=approved until first invoke reuse path marks executed.
                # find_usable accepts both approved and executed within TTL.
                grant_store.approve(gid)
            else:
                grant_store.deny(gid)
        return allowed

    return hook
