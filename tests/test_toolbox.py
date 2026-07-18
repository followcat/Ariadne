from ariadne.sandbox.toolbox import get_profile, list_profiles


def test_toolbox_profiles() -> None:
    names = {p.name for p in list_profiles()}
    assert {"minimal", "docs", "data"} <= names
    p = get_profile("docs")
    assert "pandoc" in p.packages_hint
