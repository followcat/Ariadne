"""Atelier project + session persistence."""

from pathlib import Path

import pytest

from ariadne.atelier.models import (
    slug_from_name,
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


def test_slug_from_name_chinese_and_ascii() -> None:
    assert slug_from_name("my-app") == "my-app"
    assert slug_from_name("My_App") == "my_app"
    s = slug_from_name("画画")
    assert s.startswith("atelier-")
    assert len(s) == len("atelier-") + 8
    # stable for same display name
    assert slug_from_name("画画") == s
    # mixed: ascii parts kept when enough remains
    assert slug_from_name("draw-画画").startswith("draw")
    # branch prefix
    b = slug_from_name("V字仇杀队", prefix="br")
    assert b.startswith("br-")
    assert slug_from_name("V字仇杀队", prefix="br") == b


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
