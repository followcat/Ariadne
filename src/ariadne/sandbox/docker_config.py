"""Hardened Docker run configuration + pure argv builder (always unit-testable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DockerSandboxConfig:
    """Security and resource defaults for personal Docker sandboxes."""

    image: str = "python:3.13-slim-bookworm"
    network: str = "none"  # none | bridge
    memory: str = "512m"
    cpus: str = "0.5"
    pids_limit: int = 128
    user: str = "1000:1000"
    read_only_rootfs: bool = True
    cap_drop: tuple[str, ...] = ("ALL",)
    security_opt: tuple[str, ...] = ("no-new-privileges:true",)
    runtime: str | None = None  # e.g. runsc for gVisor
    workdir: str = "/workspace"
    labels: dict[str, str] = field(default_factory=dict)


def build_run_argv(
    *,
    name: str,
    workspace: Path,
    session_dir: Path,
    config: DockerSandboxConfig,
) -> list[str]:
    """Build ``docker run`` argv for a long-lived sleep infinity container."""
    ws = str(workspace.resolve())
    sess = str(session_dir.resolve())
    cmd: list[str] = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--network",
        config.network or "none",
        "--user",
        config.user,
        "--memory",
        config.memory,
        "--cpus",
        config.cpus,
        "--pids-limit",
        str(int(config.pids_limit)),
        "--security-opt",
        "no-new-privileges:true",
    ]
    for cap in config.cap_drop:
        cmd.extend(["--cap-drop", cap])
    for opt in config.security_opt:
        if opt == "no-new-privileges:true":
            continue  # already added
        cmd.extend(["--security-opt", opt])
    if config.read_only_rootfs:
        cmd.append("--read-only")
        cmd.extend(["--tmpfs", "/tmp:rw,noexec,nosuid,size=64m"])
        cmd.extend(["--tmpfs", "/var/tmp:rw,noexec,nosuid,size=32m"])
    if config.runtime:
        cmd.extend(["--runtime", config.runtime])
    for k, v in (config.labels or {}).items():
        cmd.extend(["--label", f"{k}={v}"])
    cmd.extend(
        [
            "-v",
            f"{ws}:/workspace:rw",
            "-v",
            f"{sess}:/session:rw",
            "-w",
            config.workdir or "/workspace",
            config.image,
            "sleep",
            "infinity",
        ]
    )
    # Hard safety: never privileged, never docker.sock
    assert "--privileged" not in cmd
    assert "docker.sock" not in " ".join(cmd)
    return cmd
