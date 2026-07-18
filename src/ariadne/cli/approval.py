"""Host-side approval policies for tool invocations.

Modes (cli-shell-agent approval section):
- auto:        everything allowed (default, previous behavior)
- on-request:  write-class tools ask the user (rich Confirm), reads pass
- readonly:    write-class tools are always denied
"""

from __future__ import annotations

from typing import Any, Callable

from . import ui

# tools that mutate the filesystem / run commands / mutate user skills
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
    mode: str, *, confirm: ConfirmFn | None = None
) -> Callable[[str, dict[str, Any]], bool] | None:
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

    def hook(name: str, args: dict[str, Any]) -> bool:
        if name not in WRITE_TOOLS:
            return True
        if mode == "readonly":
            ui.print_info(f"denied (readonly): {name} {_describe(name, args)}")
            return False
        ui.console.print(f"[yellow]approval requested[/] [bold]{name}[/] {_describe(name, args)}")
        return bool(confirm("Allow?"))

    return hook
