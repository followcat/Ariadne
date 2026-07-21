"""Detect Docker engine availability for doctor / compose fastfail."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DockerCheckResult:
    docker_on_path: bool
    daemon_ok: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.docker_on_path and self.daemon_ok


def check_docker(*, timeout: float = 5.0) -> DockerCheckResult:
    if shutil.which("docker") is None:
        return DockerCheckResult(
            docker_on_path=False,
            daemon_ok=False,
            detail=(
                "docker binary not on PATH. Install Docker "
                "(https://docs.docker.com/get-docker/) or use --sandbox local "
                "for unisolated host execution."
            ),
        )
    try:
        proc = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return DockerCheckResult(
            docker_on_path=True,
            daemon_ok=False,
            detail=f"docker info failed: {exc}",
        )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        return DockerCheckResult(
            docker_on_path=True,
            daemon_ok=False,
            detail=f"docker daemon not usable: {err or 'exit ' + str(proc.returncode)}",
        )
    return DockerCheckResult(docker_on_path=True, daemon_ok=True, detail="docker ok")


def image_present(image: str, *, timeout: float = 10.0) -> bool:
    if not image or shutil.which("docker") is None:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return proc.returncode == 0
