"""Memory consolidation → L3 curated (real CuratedStore path)."""

from __future__ import annotations

from pathlib import Path

from ariadne.memory.consolidation import consolidate, propose_from_texts
from ariadne.memory.curated import CuratedStore


def test_propose_detects_preference_signals() -> None:
    cands = propose_from_texts(
        [
            "I prefer tables over prose for reports",
            "hello world is just chat",
            "请记住我总是用中文回复",
        ]
    )
    texts = [c.content for c in cands]
    assert any("prefer tables" in t.lower() for t in texts)
    assert any("中文" in t for t in texts)
    assert not any(t == "hello world is just chat" for t in texts)


def test_consolidate_dry_run_does_not_write(tmp_path: Path) -> None:
    store = CuratedStore(path=tmp_path / "curated.json")
    report = consolidate(
        store,
        session_id="s1",
        texts=["Remember: always use pytest for Python projects"],
        apply=False,
    )
    assert report["apply"] is False
    assert report["proposed_count"] >= 1
    assert report["applied_count"] == 0
    read = store.apply(action="read", scope="user", session_id="s1")
    assert read["entry_count"] == 0


def test_consolidate_apply_writes_user_curated(tmp_path: Path) -> None:
    store = CuratedStore(path=tmp_path / "curated.json")
    report = consolidate(
        store,
        session_id="s1",
        texts=["I prefer dark mode in the terminal"],
        apply=True,
    )
    assert report["applied_count"] >= 1
    read = store.apply(action="read", scope="user", session_id="s1")
    contents = " ".join(e["content"] for e in read["entries"])
    assert "prefer" in contents.lower() or "dark" in contents.lower()
    # Second apply is idempotent skip
    report2 = consolidate(
        store,
        session_id="s1",
        texts=["I prefer dark mode in the terminal"],
        apply=True,
    )
    assert report2["applied_count"] == 0
    assert any(s.get("reason") == "already_present" for s in report2["skipped"])


def test_promote_session_curated_with_apply(tmp_path: Path) -> None:
    store = CuratedStore(path=tmp_path / "curated.json")
    store.apply(
        action="add",
        content="Remember always run ruff before commit",
        scope="session",
        session_id="s9",
    )
    report = consolidate(
        store,
        session_id="s9",
        texts=None,
        include_session_curated=True,
        apply=True,
        scope="user",
    )
    assert report["applied_count"] >= 1
    user = store.apply(action="read", scope="user", session_id="s9")
    assert user["entry_count"] >= 1
