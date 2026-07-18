from __future__ import annotations

from ..errors import AriadneError, app_error
from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession


class NullSandboxSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id

    def _disabled(self, **details: object) -> AriadneError:
        return AriadneError(
            app_error(
                "ARIADNE_SANDBOX_DISABLED",
                "Sandbox backend is null; pass --sandbox local or configure ARIADNE_SANDBOX=local",
                **details,
            )
        )

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult:
        raise self._disabled(cmd=req.cmd)

    async def read_file(self, path: str) -> bytes:
        raise self._disabled(path=path)

    async def write_file(self, path: str, data: bytes) -> None:
        raise self._disabled(path=path)

    async def list_dir(self, path: str) -> list[str]:
        raise self._disabled(path=path)

    async def close(self, *, reason: str) -> None:
        return None


class NullSandbox(SandboxBackend):
    async def start(self, *, scope_key: str) -> SandboxSession:
        return NullSandboxSession(session_id=f"null-{scope_key}")
