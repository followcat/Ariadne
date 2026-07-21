"""Atelier project + session persistence."""

from pathlib import Path

import pytest

from ariadne.atelier.models import (
    Project,
    ProjectConfig,
    SessionMeta,
    SessionStatus,
    SessionType,
    load_session_meta,
    save_session_meta,
    validate_slug,
)
from ariadne.errors import AriadneError


def test_validate_slug() -> None:
    assert validate_slug("My-App") == "my-app"
    with pytest.raises(AriadneError):
        validate_slug("Bad Name!")


def test_project_roundtrip(tmp_path: Path) -> None:
    p = Project(
        id="demo",
        name="Demo",
        path=tmp_path / "demo",
        workspace_path=tmp_path / "demo" / "workspace",
        config=ProjectConfig(sandbox_profile="minimal"),
    )
    (tmp_path / "demo" / "workspace").mkdir(parents=True)
    p.save_json()
    loaded = Project.load(tmp_path / "demo")
    assert loaded.id == "demo"
    assert loaded.workspace_path == p.workspace_path
    assert loaded.config.sandbox_profile == "minimal"


def test_session_meta_roundtrip(tmp_path: Path) -> None:
    p = Project(
        id="demo",
        name="Demo",
        path=tmp_path / "demo",
        workspace_path=tmp_path / "ws",
    )
    p.path.mkdir()
    p.sessions_dir.mkdir(parents=True)
    meta = SessionMeta(
        id="main",
        project_id="demo",
        title="Main",
        type=SessionType.MAIN,
        status=SessionStatus.ACTIVE,
    )
    save_session_meta(p, meta)
    loaded = load_session_meta(p, "main")
    assert loaded.type == SessionType.MAIN
    assert loaded.status == SessionStatus.ACTIVE
