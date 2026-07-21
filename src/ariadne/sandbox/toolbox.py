from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolboxProfile:
    name: str
    description: str
    docker_image: str
    packages_hint: tuple[str, ...] = ()
    notes: str = ""


PROFILES: dict[str, ToolboxProfile] = {
    "minimal": ToolboxProfile(
        name="minimal",
        description="Bare Python/shell for local or slim container",
        docker_image="ariadne-sandbox:minimal",
        packages_hint=("bash", "coreutils", "python3", "git", "curl"),
        notes="Official minimal image; build via scripts/build_sandbox_image.sh.",
    ),
    "docs": ToolboxProfile(
        name="docs",
        description="Document conversion helpers",
        docker_image="ariadne-sandbox:minimal",
        packages_hint=("pandoc", "poppler-utils", "libreoffice"),
        notes="Install tools in a custom image or workspace venv; Ariadne does not auto-apt.",
    ),
    "data": ToolboxProfile(
        name="data",
        description="Data wrangling CLIs",
        docker_image="ariadne-sandbox:minimal",
        packages_hint=("jq", "csvkit", "sqlite3", "duckdb"),
        notes="Prefer CLI-native tools; install into /workspace venv when needed.",
    ),
}


def list_profiles() -> list[ToolboxProfile]:
    return list(PROFILES.values())


def get_profile(name: str) -> ToolboxProfile:
    key = (name or "minimal").strip().lower()
    if key not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise KeyError(f"unknown toolbox profile {name!r}; known: {known}")
    return PROFILES[key]


def profile_as_dict(p: ToolboxProfile) -> dict[str, Any]:
    return {
        "name": p.name,
        "description": p.description,
        "docker_image": p.docker_image,
        "packages_hint": list(p.packages_hint),
        "notes": p.notes,
    }
