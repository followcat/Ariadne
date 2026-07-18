import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from ariadne.sandbox.docker import DockerSandbox
from ariadne.sandbox.port import SandboxExecRequest


def _docker_usable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_usable(), reason="docker not available")
def test_docker_sandbox_optional(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "a.txt").write_text("hi\n", encoding="utf-8")
    backend = DockerSandbox(workspace=workspace, data_dir=tmp_path / "data", image="python:3.13-slim")

    async def run() -> None:
        session = await backend.start(scope_key="d1")
        try:
            result = await session.exec(SandboxExecRequest(cmd="cat a.txt", cwd="/workspace"))
            assert result.exit_code == 0
            assert "hi" in result.stdout
        finally:
            await session.close(reason="test")

    asyncio.run(run())
