from pathlib import Path

from ariadne.skills.store import SkillStore


def test_skill_load_and_search(tmp_path: Path) -> None:
    root = tmp_path / "demo_skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        """---
name: demo_skill
description: Demo skill for testing search and load.
keywords: [demo, test]
requires_tools: [sandbox_exec]
---

# Demo

Do the demo thing with sandbox_exec.
""",
        encoding="utf-8",
    )
    store = SkillStore.from_dir(tmp_path)
    assert store.get("demo_skill") is not None
    hits = store.search("demo sandbox")
    assert hits and hits[0].name == "demo_skill"
    assert "Demo" in store.index_text()
