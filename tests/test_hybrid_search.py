import asyncio
from pathlib import Path

from ariadne.memory.embeddings import HashEmbeddingProvider
from ariadne.memory.semantic import SemanticIndex
from ariadne.skills.store import SkillStore


def test_semantic_hybrid_search(tmp_path: Path) -> None:
    idx = SemanticIndex(path=tmp_path / "sem.json", embedder=HashEmbeddingProvider(dims=32))
    idx.index_turn(
        session_id="s1",
        turn_id="t1",
        user_text="write project notes about docker sandbox",
        assistant_text="created NOTES.md for docker workflow",
        tool_text="sandbox_exec wrote NOTES.md",
        summary_text="docker notes written",
        entity_ids=["doc:notes"],
    )
    idx.index_turn(
        session_id="s1",
        turn_id="t2",
        user_text="unrelated cooking recipe pasta",
        assistant_text="pasta with tomato",
        tool_text="",
        summary_text="pasta",
        entity_ids=[],
    )

    async def run():
        hits = await idx.search_hybrid(session_id="s1", query="docker sandbox notes", limit=3)
        return hits

    hits = asyncio.run(run())
    assert hits
    assert hits[0]["turn_id"] == "t1"


def test_skill_hybrid_search(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    d = root / "shell_project_notes"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        """---
name: shell_project_notes
description: Write NOTES.md for a project using shell tools.
keywords: [notes, documentation]
requires_tools: [sandbox_exec]
---

# body
""",
        encoding="utf-8",
    )
    d2 = root / "python_debug"
    d2.mkdir()
    (d2 / "SKILL.md").write_text(
        """---
name: python_debug
description: Debug Python exceptions with traceback.
keywords: [python, traceback]
requires_tools: [sandbox_exec]
---

# body
""",
        encoding="utf-8",
    )
    store = SkillStore.from_dir(root, embedder=HashEmbeddingProvider(dims=32))

    async def run():
        return await store.search_hybrid("project documentation notes", limit=2)

    hits = asyncio.run(run())
    assert hits[0].name == "shell_project_notes"
