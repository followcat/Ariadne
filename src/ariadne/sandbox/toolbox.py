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
        docker_image="python:3.13-slim",
        packages_hint=("bash", "coreutils", "python3"),
        notes="Default personal profile.",
    ),
    "docs": ToolboxProfile(
        name="docs",
        description="Document conversion helpers",
        docker_image="python:3.13-slim",
        packages_hint=("pandoc", "poppler-utils", "libreoffice"),
        notes="Install tools in image or host; Ariadne does not auto-apt.",
    ),
    "data": ToolboxProfile(
        name="data",
        description="Data wrangling CLIs",
        docker_image="python:3.13-slim",
        packages_hint=("jq", "csvkit", "sqlite3", "duckdb"),
        notes="Prefer CLI-native tools via sandbox_exec.",
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
