"""Sandbox image profiles + default resource limits (personal Docker)."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import AriadneError, app_error


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    memory: str = "512m"
    cpus: str = "0.5"
    pids_limit: int = 128


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    name: str
    description: str
    image: str
    resources: ResourceLimits
    packages_hint: tuple[str, ...] = ()
    notes: str = ""


# Official tag when built via scripts/build_sandbox_image.sh; public slim as fallback pull.
OFFICIAL_MINIMAL = "ariadne-sandbox:minimal"
PUBLIC_FALLBACK = "python:3.13-slim-bookworm"

PROFILES: dict[str, SandboxProfile] = {
    "minimal": SandboxProfile(
        name="minimal",
        description="Python + bash/git/curl for personal coding agents",
        image=OFFICIAL_MINIMAL,
        resources=ResourceLimits(memory="512m", cpus="0.5", pids_limit=128),
        packages_hint=("bash", "coreutils", "git", "curl", "python3"),
        notes="Build with scripts/build_sandbox_image.sh; falls back to slim if missing.",
    ),
    "standard": SandboxProfile(
        name="standard",
        description="Same base as minimal with higher resource defaults",
        image=OFFICIAL_MINIMAL,
        resources=ResourceLimits(memory="1g", cpus="1.0", pids_limit=128),
        packages_hint=("bash", "git", "jq", "curl"),
        notes="Use a custom image via ARIADNE_DOCKER_IMAGE for pandoc/etc.",
    ),
    "data": SandboxProfile(
        name="data",
        description="Data-oriented resource profile (install libs in workspace venv)",
        image=OFFICIAL_MINIMAL,
        resources=ResourceLimits(memory="1g", cpus="1.0", pids_limit=128),
        packages_hint=("python3", "jq"),
        notes="Prefer pip install --user into /workspace venv under network-enabled tools.",
    ),
}


def get_profile(name: str) -> SandboxProfile:
    key = (name or "minimal").strip().lower()
    if key not in PROFILES:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown sandbox profile {name!r} (use {', '.join(sorted(PROFILES))})",
            )
        )
    return PROFILES[key]


def resolve_image(*, profile: str, docker_image: str | None) -> str:
    """Explicit docker_image wins; else profile image tag."""
    if docker_image and str(docker_image).strip():
        return str(docker_image).strip()
    return get_profile(profile).image
