"""Hardened Docker sandbox backend (CLI subprocess — personal kernel)."""

from __future__ import annotations

import asyncio
import base64
import posixpath
import shutil
import time
import uuid
from pathlib import Path

from ..errors import AriadneError, app_error
from .compress import compress_observation
from .docker_check import check_docker
from .docker_config import DockerSandboxConfig, build_run_argv
from .port import SandboxBackend, SandboxExecRequest, SandboxExecResult, SandboxSession


class DockerSandboxSession:
    def __init__(
        self,
        *,
        session_id: str,
        container_id: str,
        workspace: Path,
        session_dir: Path,
        image: str,
    ) -> None:
        self.id = session_id
        self.container_id = container_id
        self.workspace = workspace
        self.session_dir = session_dir
        self.image = image

    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult:
        cwd = req.cwd or "/workspace"
        if cwd not in {"/workspace", "/session"} and not cwd.startswith(
            ("/workspace/", "/session/")
        ):
            if not cwd.startswith("/"):
                cwd = f"/workspace/{cwd}"
            else:
                raise AriadneError(
                    app_error(
                        "ARIADNE_SANDBOX_EXEC_FAILED",
                        f"invalid cwd for docker sandbox: {req.cwd}",
                    )
                )
        timeout = req.timeout_seconds if req.timeout_seconds is not None else 60.0
        cmd = [
            "docker",
            "exec",
            "-w",
            cwd,
            self.container_id,
            "sh",
            "-lc",
            req.cmd,
        ]
        started = time.perf_counter()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
        except FileNotFoundError as exc:
            raise AriadneError(
                app_error("ARIADNE_SANDBOX_EXEC_FAILED", "docker binary not found", cmd=req.cmd)
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"{type(exc).__name__}: {exc}",
                    cmd=req.cmd,
                )
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

    @staticmethod
    def _container_path(path: str) -> str:
        raw = (path or "").strip()
        if not raw:
            raise AriadneError(app_error("ARIADNE_SANDBOX_EXEC_FAILED", "path is required"))
        if not raw.startswith("/"):
            raw = f"/workspace/{raw}"
        norm = posixpath.normpath(raw)
        if norm not in {"/workspace", "/session"} and not norm.startswith(
            ("/workspace/", "/session/")
        ):
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"path escapes sandbox roots: {path!r}",
                )
            )
        return norm

    async def _run_capture(
        self, argv: list[str], *, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate(input=stdin)
        return int(proc.returncode if proc.returncode is not None else -1), out_b, err_b

    async def read_file(self, path: str) -> bytes:
        cpath = self._container_path(path)
        code, out_b, err_b = await self._run_capture(
            ["docker", "exec", self.container_id, "cat", "--", cpath]
        )
        if code != 0:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"read_file failed: {err_b.decode('utf-8', errors='replace')[:200]}",
                    path=path,
                )
            )
        return out_b

    async def write_file(self, path: str, data: bytes) -> None:
        cpath = self._container_path(path)
        payload = base64.b64encode(data)
        code, _, err_b = await self._run_capture(
            [
                "docker",
                "exec",
                "-i",
                self.container_id,
                "sh",
                "-c",
                f"mkdir -p -- \"$(dirname -- '{cpath}')\" && base64 -d > '{cpath}'",
            ],
            stdin=payload,
        )
        if code != 0:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"write_file failed: {err_b.decode('utf-8', errors='replace')[:200]}",
                    path=path,
                )
            )

    async def list_dir(self, path: str) -> list[str]:
        cpath = self._container_path(path)
        code, out_b, err_b = await self._run_capture(
            ["docker", "exec", self.container_id, "ls", "-1A", "--", cpath]
        )
        if code != 0:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"list_dir failed: {err_b.decode('utf-8', errors='replace')[:200]}",
                    path=path,
                )
            )
        return sorted(
            line for line in out_b.decode("utf-8", errors="replace").splitlines() if line.strip()
        )

    async def close(self, *, reason: str) -> None:
        _ = reason
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            self.container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        shutil.rmtree(self.session_dir, ignore_errors=True)


class DockerSandbox(SandboxBackend):
    """Hardened Docker backend: one container per scope, docker CLI subprocess."""

    def __init__(
        self,
        *,
        workspace: Path,
        data_dir: Path,
        image: str | None = None,
        network: str = "none",
        config: DockerSandboxConfig | None = None,
        require_daemon: bool = True,
    ) -> None:
        if shutil.which("docker") is None:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    "docker not available on PATH. Install Docker or use --sandbox local.",
                )
            )
        if require_daemon:
            chk = check_docker()
            if not chk.ok:
                raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", chk.detail))
        self.workspace = workspace.resolve()
        self.data_dir = data_dir.resolve()
        if config is None:
            config = DockerSandboxConfig(
                image=image or "python:3.13-slim-bookworm",
                network=network or "none",
            )
        elif image:
            config = DockerSandboxConfig(
                image=image,
                network=config.network,
                memory=config.memory,
                cpus=config.cpus,
                pids_limit=config.pids_limit,
                user=config.user,
                read_only_rootfs=config.read_only_rootfs,
                cap_drop=config.cap_drop,
                security_opt=config.security_opt,
                runtime=config.runtime,
                workdir=config.workdir,
                labels=dict(config.labels),
            )
        self.config = config
        self.image = config.image
        self.network = config.network
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start(self, *, scope_key: str) -> SandboxSession:
        safe = (
            "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in scope_key)[:60] or "scope"
        )
        session_dir = self.data_dir / "sandbox" / safe / "session"
        session_dir.mkdir(parents=True, exist_ok=True)
        name = f"ariadne-{safe}-{uuid.uuid4().hex[:8]}"
        labels = dict(self.config.labels)
        labels.setdefault("ariadne.scope", safe)
        labels.setdefault("ariadne.managed", "1")
        cfg = DockerSandboxConfig(
            image=self.config.image,
            network=self.config.network,
            memory=self.config.memory,
            cpus=self.config.cpus,
            pids_limit=self.config.pids_limit,
            user=self.config.user,
            read_only_rootfs=self.config.read_only_rootfs,
            cap_drop=self.config.cap_drop,
            security_opt=self.config.security_opt,
            runtime=self.config.runtime,
            workdir=self.config.workdir,
            labels=labels,
        )
        cmd = build_run_argv(
            name=name,
            workspace=self.workspace,
            session_dir=session_dir,
            config=cfg,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out_b, err_b = await proc.communicate()
        if proc.returncode != 0:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    "failed to start docker sandbox",
                    stderr=err_b.decode("utf-8", errors="replace")[:500],
                )
            )
        container_id = out_b.decode().strip() or name
        return DockerSandboxSession(
            session_id=f"docker-{safe}",
            container_id=container_id,
            workspace=self.workspace,
            session_dir=session_dir,
            image=self.config.image,
        )
