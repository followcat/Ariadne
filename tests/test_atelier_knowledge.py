from pathlib import Path

from ariadne.atelier.knowledge import (
    apply_updates,
    extract_knowledge_heuristic,
    heuristic_refresh,
    knowledge_template,
    list_knowledge_history,
    write_knowledge,
    KnowledgeUpdate,
    KnowledgeUpdateItem,
)
from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.models import Project


def test_heuristic_and_history(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    code = tmp_path / "src"
    code.mkdir()
    (code / "main.py").write_text("x=1\n", encoding="utf-8")
    (code / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    from ariadne.atelier.knowledge import read_knowledge

    proj = mgr.create_project("k1", from_path=code)
    text = read_knowledge(proj)
    assert "Python" in text or "技术栈" in text
    write_knowledge(proj, knowledge_template("k1") + "\n- extra\n", session_id="t")
    write_knowledge(proj, heuristic_refresh(proj), session_id="t2")
    hist = list_knowledge_history(proj)
    assert len(hist) >= 1


def test_extract_and_apply() -> None:
    upd = extract_knowledge_heuristic(
        [{"role": "user", "content": "我们决定使用 ruff 做 lint"}]
    )
    assert upd.has_update
    base = knowledge_template("demo")
    new = apply_updates(
        base,
        KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(section="关键决策", type="add", new_text="使用 ruff")
            ],
        ),
    )
    assert "ruff" in new
