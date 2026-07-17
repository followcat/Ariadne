from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class SandboxExecRequest:
    cmd: str
    cwd: str = "/workspace"
    timeout_seconds: float | None = 60.0
    env: dict[str, str] | None = None
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 64_000


@dataclass(slots=True)
class SandboxExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    compressed: bool = False
    duration_ms: int = 0
    cwd: str = "/workspace"


class SandboxSession(Protocol):
    id: str

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult: ...

    async def close(self, *, reason: str) -> None: ...


class SandboxBackend(Protocol):
    async def start(self, *, scope_key: str) -> SandboxSession: ...
