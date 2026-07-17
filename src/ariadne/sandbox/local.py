from __future__ import annotations

import asyncio
import os
import shlex
import time
from pathlib import Path

from ..errors import AriadneError, app_error
from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession


class LocalWorkdirSession:
    """Subprocess sandbox rooted at host workspace + ephemeral session dir."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace: Path,
        session_dir: Path,
    ) -> None:
        self.id = session_id
        self.workspace = workspace.resolve()
        self.session_dir = session_dir.resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _map_cwd(self, cwd: str) -> Path:
        raw = (cwd or "/workspace").strip() or "/workspace"
        if raw in {"/workspace", "workspace"}:
            return self.workspace
        if raw in {"/session", "session"}:
            return self.session_dir
        if raw.startswith("/workspace/"):
            rel = raw[len("/workspace/") :]
            return self._safe_join(self.workspace, rel)
        if raw.startswith("/session/"):
            rel = raw[len("/session/") :]
            return self._safe_join(self.session_dir, rel)
        # relative path -> workspace
        if not raw.startswith("/"):
            return self._safe_join(self.workspace, raw)
        raise AriadneError(
            app_error(
                "ARIADNE_SANDBOX_EXEC_FAILED",
                f"cwd must be under /workspace or /session, got {cwd!r}",
            )
        )

    @staticmethod
    def _safe_join(root: Path, rel: str) -> Path:
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"path escapes sandbox root: {rel!r}",
                )
            )
        return target

    def _truncate(self, text: str, limit: int) -> tuple[str, bool]:
        raw = text.encode("utf-8", errors="replace")
        if len(raw) <= limit:
            return text, False
        head = raw[: max(limit // 2, 1)]
        tail = raw[-max(limit // 2, 1) :]
        marker = b"\n[ariadne: output truncated; kept head+tail]\n"
        return (head + marker + tail).decode("utf-8", errors="replace"), True

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult:
        cwd_path = self._map_cwd(req.cwd)
        cwd_path.mkdir(parents=True, exist_ok=True)
        timeout = req.timeout_seconds if req.timeout_seconds is not None else 60.0
        env = os.environ.copy()
        # Do not forward proxy noise into child by default for determinism of some tools.
        for key in list(env):
            if "proxy" in key.lower():
                env.pop(key, None)
        if req.env:
            env.update(req.env)
        env["ARIADNE_WORKSPACE"] = str(self.workspace)
        env["ARIADNE_SESSION_DIR"] = str(self.session_dir)

        # Provide shell aliases for virtual roots via env + note in prompt, not bind mounts.
        started = time.perf_counter()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_shell(
                req.cmd,
                cwd=str(cwd_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
        except AriadneError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(
                app_error("ARIADNE_SANDBOX_EXEC_FAILED", f"{type(exc).__name__}: {exc}", cmd=req.cmd)
            ) from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout, t1 = self._truncate(stdout_b.decode("utf-8", errors="replace"), req.max_stdout_bytes)
        stderr, t2 = self._truncate(stderr_b.decode("utf-8", errors="replace"), req.max_stderr_bytes)
        if timed_out and not stderr:
            stderr = f"[ariadne: command timed out after {timeout}s]"
        return SandboxExecResult(
            exit_code=int(proc.returncode if proc.returncode is not None else -1),
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=t1 or t2,
            duration_ms=duration_ms,
            cwd=req.cwd or "/workspace",
        )

    async def close(self, *, reason: str) -> None:
        # session_dir kept for debugging; wipe optional later
        return None


class LocalWorkdirSandbox(SandboxBackend):
    def __init__(self, *, workspace: Path, data_dir: Path) -> None:
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start(self, *, scope_key: str) -> SandboxSession:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in scope_key)[:80] or "scope"
        session_dir = self.data_dir / "sandbox" / safe / "session"
        return LocalWorkdirSession(
            session_id=f"local-{safe}",
            workspace=self.workspace,
            session_dir=session_dir,
        )
