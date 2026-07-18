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
    old = await _read_text(sandbox, path)
    await sandbox.write_file(path, content.encode("utf-8"))
    diff = _unified_diff(path, old, content)
    return {"path": path, "bytes": len(content.encode("utf-8")), "diff": diff, "created": not bool(old)}


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


def register_file_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="sandbox_read_file",
            catalog_description="read a workspace file",
            description=(
                "Read a file from the sandbox. Paths use the sandbox contract: "
                "/workspace (durable project root) or /session (scratch). "
                "Prefer this over cat when you need exact file content."
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
                "Overwrite a file in the sandbox with full content. Returns a "
                "unified diff against the previous content. For partial edits "
                "prefer sandbox_edit_file. Path must be under /workspace or /session."
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
