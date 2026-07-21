"""In-process Runtime Agent: policy + audit around sandbox session ops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import AriadneError, app_error
from .policy import CommandPolicy, EgressPolicy
from .port import SandboxExecRequest, SandboxSession


@dataclass
class RuntimeAgent:
    """Codex-style mediation without a separate daemon process."""

    session: SandboxSession | None = None
    command_policy: CommandPolicy | None = None
    egress_policy: EgressPolicy | None = None

    def bind(self, session: SandboxSession | None) -> None:
        self.session = session

    async def read_file(self, path: str) -> bytes:
        if self.session is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        return await self.session.read_file(path)

    async def write_file(self, path: str, data: bytes) -> None:
        if self.session is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        await self.session.write_file(path, data)

    async def list_dir(self, path: str) -> list[str]:
        if self.session is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        return await self.session.list_dir(path)

    async def execute_shell(
        self,
        cmd: str,
        *,
        cwd: str = "/workspace",
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        if self.session is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        policy = self.command_policy or CommandPolicy()
        ok, reason = policy.is_allowed(cmd)
        if not ok:
            policy.audit(
                {"action": "shell_deny", "cmd": cmd, "cwd": cwd, "reason": reason}
            )
            raise AriadneError(
                app_error("ARIADNE_TOOL_DENIED", f"command denied: {reason}", cmd=cmd)
            )
        result = await self.session.exec(
            SandboxExecRequest(cmd=cmd, cwd=cwd, timeout_seconds=timeout_seconds)
        )
        out = {
            "exit_code": result.exit_code,
            "stdout": policy.redact(result.stdout),
            "stderr": policy.redact(result.stderr),
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "compressed": result.compressed,
            "duration_ms": result.duration_ms,
            "cwd": result.cwd,
        }
        policy.audit(
            {
                "action": "shell_ok",
                "cmd": cmd,
                "cwd": cwd,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
            }
        )
        return out
