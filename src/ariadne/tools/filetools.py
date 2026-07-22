"""Model-facing file tools over the sandbox session FS API.

codex apply_patch / claude Edit equivalents for Ariadne: read / write /
exact-replace edit with unified diffs. Handlers use the SandboxSession
FS API (sandbox/port.py) — no shell string protocols.
"""

from __future__ import annotations

import difflib
from typing import Any

from ..errors import AriadneError, app_error
from .registry import ToolContext, ToolRegistry, ToolSpec


def _unified_diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
    )
    return "\n".join(lines)


def _require_sandbox(ctx: ToolContext):
    if ctx.sandbox is None:
        raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
    return ctx.sandbox


async def _read_text(sandbox: Any, path: str) -> str:
    try:
        data = await sandbox.read_file(path)
    except AriadneError as exc:
        if "not found" in exc.error.message:
            return ""
        raise
    return data.decode("utf-8", errors="replace")


async def sandbox_read_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    path = str(args.get("path") or "").strip()
    if not path:
        raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "path is required"))
    data = await sandbox.read_file(path)
    text = data.decode("utf-8", errors="replace")
    return {"path": path, "bytes": len(data), "content": text}


async def sandbox_write_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    path = str(args.get("path") or "").strip()
    if not path:
        raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "path is required"))
    content = str(args.get("content") or "")
    # Soft guard: huge one-shot rewrites often get model-truncated mid-file and thrash.
    max_chars = 24_000
    warning = ""
    if len(content) > max_chars:
        raise AriadneError(
            app_error(
                "ARIADNE_INVALID_TOOL_ARGS",
                f"content too large ({len(content)} chars; max {max_chars}). "
                "Write a smaller file, or use sandbox_edit_file for patches. "
                "Do not paste a whole minified multi-KB script in one call.",
                path=path,
                size=len(content),
            )
        )
    if len(content) > 12_000:
        warning = (
            f"large write ({len(content)} chars): prefer smaller chunks next time "
            "to avoid truncation/thrash"
        )
    old = await _read_text(sandbox, path)
    await sandbox.write_file(path, content.encode("utf-8"))
    diff = _unified_diff(path, old, content)
    out: dict[str, Any] = {
        "path": path,
        "bytes": len(content.encode("utf-8")),
        "diff": diff,
        "created": not bool(old),
    }
    if warning:
        out["warning"] = warning
    return out


async def sandbox_edit_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    path = str(args.get("path") or "").strip()
    old_string = str(args.get("old_string") or "")
    new_string = str(args.get("new_string") or "")
    if not path or not old_string:
        raise AriadneError(
            app_error("ARIADNE_INVALID_TOOL_ARGS", "path and old_string are required")
        )
    data = await sandbox.read_file(path)
    old = data.decode("utf-8", errors="replace")
    occurrences = old.count(old_string)
    if occurrences != 1:
        raise AriadneError(
            app_error(
                "ARIADNE_INVALID_TOOL_ARGS",
                f"old_string must match exactly once, found {occurrences}",
                path=path,
                occurrences=occurrences,
            )
        )
    new = old.replace(old_string, new_string, 1)
    await sandbox.write_file(path, new.encode("utf-8"))
    return {"path": path, "bytes": len(new.encode("utf-8")), "diff": _unified_diff(path, old, new)}


async def sandbox_list_dir(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sandbox = _require_sandbox(ctx)
    path = str(args.get("path") or "/workspace").strip() or "/workspace"
    entries = await sandbox.list_dir(path)
    return {"path": path, "entries": entries, "count": len(entries)}


async def sandbox_delete_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Delete via shell under policy when RuntimeAgent present; else exec rm."""
    path = str(args.get("path") or "").strip()
    if not path:
        raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "path is required"))
    # Prefer shell rm through policy so dangerous paths can be denied
    cmd = f"rm -f -- {path!s}"
    # quote-safe: use simple shell quoting
    import shlex

    cmd = "rm -f -- " + shlex.quote(path)
    if ctx.runtime_agent is not None:
        out = await ctx.runtime_agent.execute_shell(cmd, cwd="/workspace")
        return {"path": path, "deleted": out.get("exit_code") == 0, **out}
    sandbox = _require_sandbox(ctx)
    from ..sandbox.port import SandboxExecRequest

    result = await sandbox.exec(SandboxExecRequest(cmd=cmd, cwd="/workspace"))
    return {
        "path": path,
        "deleted": result.exit_code == 0,
        "exit_code": result.exit_code,
        "stderr": result.stderr,
    }


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="sandbox_read_file",
            catalog_description="read a workspace file",
            description=(
                "PREFERRED file read (semantic tool). Paths: /workspace (durable) or "
                "/session (scratch). Prefer this over sandbox_exec/cat for exact content."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=sandbox_read_file,
        )
    )
    registry.register(
        ToolSpec(
            name="sandbox_write_file",
            catalog_description="write (overwrite) a workspace file",
            description=(
                "PREFERRED full-file write (semantic tool). Returns unified diff. "
                "For partial edits use sandbox_edit_file. Paths under /workspace or /session. "
                "Prefer this over echo/printf via sandbox_exec."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=sandbox_write_file,
        )
    )
    registry.register(
        ToolSpec(
            name="sandbox_edit_file",
            catalog_description="exact-replace edit of a workspace file",
            description=(
                "Replace old_string with new_string in a file. old_string must "
                "match EXACTLY once (include enough context to be unique) or the "
                "call fails. Read the file first. Returns a unified diff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            handler=sandbox_edit_file,
        )
    )
    registry.register(
        ToolSpec(
            name="sandbox_list_dir",
            catalog_description="list directory in sandbox",
            description=(
                "PREFERRED directory listing (semantic tool). Path under /workspace or /session. "
                "Prefer this over ls via sandbox_exec."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
                "additionalProperties": False,
            },
            handler=sandbox_list_dir,
        )
    )
    registry.register(
        ToolSpec(
            name="sandbox_delete_file",
            catalog_description="delete a sandbox file",
            description=(
                "Delete a file under /workspace or /session. Prefer this over rm via sandbox_exec. "
                "Subject to command policy when RuntimeAgent is active."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=sandbox_delete_file,
        )
    )
