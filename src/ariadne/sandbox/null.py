from __future__ import annotations

from ..errors import AriadneError, app_error
from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession


class NullSandboxSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult:
        raise AriadneError(
            app_error(
                "ARIADNE_SANDBOX_DISABLED",
                "Sandbox backend is null; pass --sandbox local or configure ARIADNE_SANDBOX=local",
                cmd=req.cmd,
            )
        )

    async def close(self, *, reason: str) -> None:
        return None


class NullSandbox(SandboxBackend):
    async def start(self, *, scope_key: str) -> SandboxSession:
        return NullSandboxSession(session_id=f"null-{scope_key}")
