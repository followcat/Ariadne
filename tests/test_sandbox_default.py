"""Product default sandbox=docker + config knobs."""

from pathlib import Path

import pytest

from ariadne.config import load_settings
from ariadne.sandbox.profiles import get_profile, resolve_image


def test_default_sandbox_is_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIADNE_SANDBOX", raising=False)
    settings = load_settings(workspace=tmp_path / "ws", force_workspace=True)
    assert settings.sandbox == "docker"
    assert settings.sandbox_network == "none"
    assert settings.sandbox_profile == "minimal"


def test_resolve_image_override(tmp_path: Path) -> None:
    assert resolve_image(profile="minimal", docker_image="my:tag") == "my:tag"
    assert "minimal" in get_profile("minimal").image or "python" in get_profile("minimal").image
