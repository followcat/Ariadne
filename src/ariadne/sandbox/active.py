from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession


@dataclass
class ActiveSessionHandle:
    key: str
    session: SandboxSession
    backend_name: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used = time.time()


class ActiveSessionManager:
    """Reuse sandbox sessions across turns with idle/max TTL."""

    def __init__(
        self,
        backend: SandboxBackend,
        *,
        idle_ttl_seconds: float = 600.0,
        max_ttl_seconds: float = 3600.0,
        backend_name: str = "local",
    ) -> None:
        self.backend = backend
        self.idle_ttl_seconds = idle_ttl_seconds
        self.max_ttl_seconds = max_ttl_seconds
        self.backend_name = backend_name
        self._sessions: dict[str, ActiveSessionHandle] = {}

    async def get_or_start(self, *, session_id: str) -> SandboxSession:
        await self.reap()
        key = session_id
        handle = self._sessions.get(key)
        if handle is not None:
            handle.touch()
            return handle.session
        session = await self.backend.start(scope_key=f"active-{session_id}")
        self._sessions[key] = ActiveSessionHandle(
            key=key, session=session, backend_name=self.backend_name
        )
        return session

    async def release_turn(self, *, session_id: str, keep_alive: bool) -> None:
        if keep_alive:
            handle = self._sessions.get(session_id)
            if handle:
                handle.touch()
            return
        handle = self._sessions.pop(session_id, None)
        if handle is not None:
            await handle.session.close(reason="per_turn_close")

    async def close(self, session_id: str, *, reason: str = "manual") -> None:
        handle = self._sessions.pop(session_id, None)
        if handle is not None:
            await handle.session.close(reason=reason)

    async def clear_session_files(self, session_id: str) -> bool:
        handle = self._sessions.get(session_id)
        if handle is None:
            return False
        result = await handle.session.exec(
            SandboxExecRequest(
                cmd="find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +",
                cwd="/session",
            )
        )
        if result.exit_code != 0:
            raise RuntimeError(
                f"clear_session_files failed exit={result.exit_code} stderr={result.stderr[:200]}"
            )
        return True

    async def reap(self) -> list[str]:
        now = time.time()
        closed: list[str] = []
        for key, handle in list(self._sessions.items()):
            idle = now - handle.last_used
            age = now - handle.created_at
            if idle > self.idle_ttl_seconds or age > self.max_ttl_seconds:
                await handle.session.close(reason="ttl_expired")
                self._sessions.pop(key, None)
                closed.append(key)
        return closed

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "count": len(self._sessions),
            "sessions": {
                k: {
                    "idle_s": round(now - h.last_used, 2),
                    "age_s": round(now - h.created_at, 2),
                    "backend": h.backend_name,
                }
                for k, h in self._sessions.items()
            },
        }
