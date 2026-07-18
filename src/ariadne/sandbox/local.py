from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from ..errors import AriadneError, app_error
from .compress import compress_observation
from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession

# sandbox-v1 §4.2: env allowlist only — no host process secrets by default.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "TERM",
    "TZ",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
)


class LocalWorkdirSession:
    def __init__(
        self,
        *,
        session_id: str,
        workspace: Path,
        session_dir: Path,
        env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST,
    ) -> None:
        self.id = session_id
        self.workspace = workspace.resolve()
        self.session_dir = session_dir.resolve()
        self.env_allowlist = env_allowlist
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def _resolve(self, path: str) -> Path:
        raw = (path or "/workspace").strip() or "/workspace"
        if raw in {"/workspace", "workspace"}:
            return self.workspace
        if raw in {"/session", "session"}:
            return self.session_dir
        if raw.startswith("/workspace/"):
            return self._safe_join(self.workspace, raw[len("/workspace/") :])
        if raw.startswith("/session/"):
            return self._safe_join(self.session_dir, raw[len("/session/") :])
        if not raw.startswith("/"):
            return self._safe_join(self.workspace, raw)
        raise AriadneError(
            app_error(
                "ARIADNE_SANDBOX_EXEC_FAILED",
                f"path must be under /workspace or /session, got {path!r}",
            )
        )

    def _map_cwd(self, cwd: str) -> Path:
        return self._resolve(cwd)

    @staticmethod
    def _safe_join(root: Path, rel: str) -> Path:
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            raise AriadneError(
                app_error("ARIADNE_SANDBOX_EXEC_FAILED", f"path escapes sandbox root: {rel!r}")
            )
        return target

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult:
        cwd_path = self._map_cwd(req.cwd)
        cwd_path.mkdir(parents=True, exist_ok=True)
        timeout = req.timeout_seconds if req.timeout_seconds is not None else 60.0
        # env allowlist only: never forward host secrets into the sandbox
        env: dict[str, str] = {}
        for key in self.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        for key, val in os.environ.items():
            if key.startswith("LC_"):
                env[key] = val
        if req.env:
            env.update(req.env)
        env["ARIADNE_WORKSPACE"] = str(self.workspace)
        env["ARIADNE_SESSION_DIR"] = str(self.session_dir)

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
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if timed_out and not stderr:
            stderr = f"[ariadne: command timed out after {timeout}s]"
        comp = compress_observation(
            stdout=stdout,
            stderr=stderr,
            max_stdout_bytes=req.max_stdout_bytes,
            max_stderr_bytes=req.max_stderr_bytes,
        )
        return SandboxExecResult(
            exit_code=int(proc.returncode if proc.returncode is not None else -1),
            stdout=comp.stdout,
            stderr=comp.stderr,
            timed_out=timed_out,
            truncated=comp.truncated,
            compressed=comp.compressed,
            duration_ms=duration_ms,
            cwd=req.cwd or "/workspace",
        )

    async def read_file(self, path: str) -> bytes:
        target = self._resolve(path)
        if not target.is_file():
            raise AriadneError(
                app_error("ARIADNE_SANDBOX_EXEC_FAILED", f"file not found: {path!r}")
            )
        return target.read_bytes()

    async def write_file(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_dir(self, path: str) -> list[str]:
        target = self._resolve(path)
        if not target.is_dir():
            raise AriadneError(
                app_error("ARIADNE_SANDBOX_EXEC_FAILED", f"directory not found: {path!r}")
            )
        return sorted(p.name for p in target.iterdir())

    async def close(self, *, reason: str) -> None:
        # /session is scratch: removed on close (per_turn default and TTL closes)
        shutil.rmtree(self.session_dir, ignore_errors=True)


class LocalWorkdirSandbox(SandboxBackend):
    def __init__(
        self,
        *,
        workspace: Path,
        data_dir: Path,
        env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST,
    ) -> None:
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        self.env_allowlist = env_allowlist
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start(self, *, scope_key: str) -> SandboxSession:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in scope_key)[:80] or "scope"
        session_dir = self.data_dir / "sandbox" / safe / "session"
        return LocalWorkdirSession(
            session_id=f"local-{safe}",
            workspace=self.workspace,
            session_dir=session_dir,
            env_allowlist=self.env_allowlist,
        )
