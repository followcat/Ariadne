from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..errors import AriadneError, app_error
from ..sandbox.port import SandboxExecRequest, SandboxSession


ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]


@dataclass(slots=True)
class ToolContext:
    session_id: str
    sandbox: SandboxSession | None


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    catalog_description: str = ""

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def list_openai_tools(self) -> list[dict[str, Any]]:
        return [spec.openai_tool() for spec in self.tools.values()]

    def catalog_text(self) -> str:
        lines = []
        for spec in self.tools.values():
            phrase = spec.catalog_description or spec.description.split(".")[0]
            lines.append(f"- {spec.name}: {phrase}")
        return "\n".join(lines)

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self.tools.get(name)
        if spec is None:
            raise AriadneError(app_error("ARIADNE_UNKNOWN_TOOL", f"Unknown tool: {name}", name=name))
        return await spec.handler(arguments, ctx)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def sandbox_exec(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.sandbox is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "cmd is required"))
        cwd = str(args.get("cwd") or "/workspace")
        timeout = args.get("timeout_seconds")
        timeout_f = float(timeout) if timeout is not None else 60.0
        result = await ctx.sandbox.exec(
            SandboxExecRequest(cmd=cmd, cwd=cwd, timeout_seconds=timeout_f)
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
            "cwd": result.cwd,
        }

    registry.register(
        ToolSpec(
            name="sandbox_exec",
            catalog_description="run shell command in workspace",
            description=(
                "Run a shell command in the local project sandbox. "
                "Default cwd is the project root (/workspace). Prefer relative paths (e.g. NOTES.md). "
                "Use cwd='/session' for ephemeral scratch ($ARIADNE_SESSION_DIR). "
                "Shell state (cd/export) does not persist across calls; write files to persist. "
                "Prefer non-interactive commands. Large outputs may be truncated with markers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Virtual cwd: /workspace or /session (default /workspace).",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout seconds (default 60).",
                    },
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
            handler=sandbox_exec,
        )
    )
    return registry


def dumps_tool_output(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
