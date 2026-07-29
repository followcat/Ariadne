from pathlib import Path

from ariadne.atelier.knowledge import (
    KnowledgeUpdate,
    KnowledgeUpdateItem,
    _parse_llm_json,
    apply_updates,
    extract_knowledge_heuristic,
    extract_knowledge_llm,
    heuristic_refresh,
    knowledge_for_inject,
    knowledge_template,
    list_knowledge_history,
    write_knowledge,
)
from ariadne.atelier.manager import AtelierManager


def test_template_is_short_agents_style() -> None:
    t = knowledge_template("demo")
    assert "本坊" in t or "运作" in t
    assert "关键路径" in t
    assert "/workspace" in t
    assert "KNOWLEDGE.md" in t
    assert "注意" in t
    assert len(t) < 1200


def test_knowledge_prefer_workspace_when_root_thin(tmp_path: Path) -> None:
    from ariadne.atelier.knowledge import knowledge_for_inject, sync_knowledge_from_workspace_if_empty
    from ariadne.atelier.models import Project

    root = tmp_path / "atelier-x"
    ws = root / "workspace"
    ws.mkdir(parents=True)
    (root / ".ariadne" / "knowledge_history").mkdir(parents=True)
    # polluted root (old auto-extract junk)
    (root / "KNOWLEDGE.md").write_text(
        '# x\n\n## 关键决策\n- "has_update": true,\n- "updates": [\n- "section": "技术栈",\n',
        encoding="utf-8",
    )
    rich = "# 画画\n\n## 决策与约定\n- Canvas 蜡笔粒子\n- 入口 index.html\n"
    (ws / "KNOWLEDGE.md").write_text(rich, encoding="utf-8")
    proj = Project(id="x", name="x", path=root, workspace_path=ws)
    inj = knowledge_for_inject(proj)
    assert "蜡笔" in inj or "index.html" in inj
    assert '"has_update"' not in inj
    # inject already promotes; second call is no-op if same
    assert "蜡笔" in (root / "KNOWLEDGE.md").read_text(encoding="utf-8")


def test_promote_workspace_handbook_over_scaffold(tmp_path: Path) -> None:
    """Agent rewrote /workspace/KNOWLEDGE.md; panel root was still scaffold."""
    from ariadne.atelier.knowledge import (
        read_knowledge,
        sync_knowledge_from_workspace_if_empty,
    )
    from ariadne.atelier.models import Project

    root = tmp_path / "atelier-y"
    ws = root / "workspace"
    ws.mkdir(parents=True)
    (root / ".ariadne" / "knowledge_history").mkdir(parents=True)
    scaffold = (
        "# 架构整理和描述\n\n"
        "> 小本本草稿（扫了一眼文件夹自动填的，随便改）。\n\n"
        "## 我想记住的\n"
        "- 大概用了: （未能从文件树推断，请手写）\n"
        "- （还没想好也可以先空着）\n\n"
        "## 随手记\n"
        "- 扫到大约 0 个文件 · 2026-07-24\n"
    )
    handbook = (
        "# 架构整理和描述 · 作坊运作手册\n\n"
        "## 本坊怎么运作\n"
        "- 主线定策略，旁支动手\n\n"
        "## 关键路径\n"
        "- 工作区: `/workspace`\n"
        "- 便签: 作坊根 KNOWLEDGE.md\n\n"
        "## 注意\n"
        "- 别把 demo 坑写进便签\n"
    )
    (root / "KNOWLEDGE.md").write_text(scaffold, encoding="utf-8")
    (ws / "KNOWLEDGE.md").write_text(handbook, encoding="utf-8")
    proj = Project(id="y", name="y", path=root, workspace_path=ws)
    assert sync_knowledge_from_workspace_if_empty(proj) is True
    body = read_knowledge(proj)
    assert "作坊运作手册" in body or "本坊怎么运作" in body
    assert "扫了一眼文件夹" not in body


def test_heuristic_and_history(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    code = tmp_path / "src"
    code.mkdir()
    (code / "main.py").write_text("x=1\n", encoding="utf-8")
    (code / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    from ariadne.atelier.knowledge import read_knowledge

    proj = mgr.create_project("k1", from_path=code)
    text = read_knowledge(proj)
    assert "Python" in text or "决策" in text
    write_knowledge(proj, knowledge_template("k1") + "\n- extra\n", session_id="t")
    write_knowledge(proj, heuristic_refresh(proj), session_id="t2")
    hist = list_knowledge_history(proj)
    assert len(hist) >= 1


def test_inject_truncates() -> None:
    from ariadne.atelier.models import Project, ProjectConfig

    p = Project(
        id="x",
        name="x",
        path=Path("/tmp/x"),
        workspace_path=Path("/tmp/x/w"),
        config=ProjectConfig(),
    )
    # force long content via monkey by writing through template helper path
    long = "# x\n\n" + ("- line\n" * 2000)
    # read_knowledge falls back if no file; use knowledge_for_inject on a fake with file
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        proj = Project(
            id="y",
            name="y",
            path=root,
            workspace_path=root / "w",
        )
        (root / "w").mkdir()
        write_knowledge(proj, long)
        inj = knowledge_for_inject(proj, limit=500)
        assert len(inj) <= 520
        assert "截断" in inj or len(inj) <= 500


def test_apply_modify_and_remove() -> None:
    base = knowledge_template("demo")
    with_item = apply_updates(
        base,
        KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(
                    section="决策与约定", type="add", new_text="始终使用 black 格式化"
                )
            ],
        ),
    )
    assert "black" in with_item

    modified = apply_updates(
        with_item,
        KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(
                    section="决策与约定",
                    type="modify",
                    old_text="black",
                    new_text="始终使用 ruff format",
                )
            ],
        ),
    )
    assert "ruff format" in modified

    removed = apply_updates(
        modified,
        KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(
                    section="决策与约定",
                    type="remove",
                    old_text="ruff format",
                )
            ],
        ),
    )
    assert "ruff format" not in removed


def test_extract_helpers_still_work_opt_in() -> None:
    upd = extract_knowledge_heuristic(
        [{"role": "user", "content": "我们决定使用 ruff 做 lint"}]
    )
    assert upd.has_update
    raw = '{"has_update": true, "updates": [{"section": "决策与约定", "type": "add", "new_text": "JWT"}]}'
    assert _parse_llm_json(raw).has_update


def test_extract_knowledge_llm_with_complete() -> None:
    import asyncio

    async def fake_complete(_prompt: str) -> str:
        return (
            '{"has_update": true, "updates": ['
            '{"section": "决策与约定", "type": "add", "new_text": "JWT auth", '
            '"old_text": "", "evidence": "decided JWT"}]}'
        )

    async def run() -> None:
        upd = await extract_knowledge_llm(
            [{"role": "user", "content": "我们决定使用 JWT"}],
            knowledge_template("x"),
            complete=fake_complete,
        )
        assert upd.has_update

    asyncio.run(run())


def test_workspace_tree_lists_files_when_host_path_contains_dot_ariadne(
    tmp_path: Path,
) -> None:
    """Web ateliers live under …/.ariadne/…/workspace — must not skip all files."""
    from ariadne.atelier.knowledge import workspace_tree_lines

    # Mimic web data layout: host path contains a ".ariadne" parent segment
    ws = tmp_path / ".ariadne" / "web" / "users" / "u" / "ateliers" / "x" / "workspace"
    ws.mkdir(parents=True)
    (ws / "KNOWLEDGE.md").write_text("# k\n", encoding="utf-8")
    (ws / "aiflow-core-architecture.md").write_text("# doc\n", encoding="utf-8")
    (ws / "aiflow-core-architecture.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>\n', encoding="utf-8"
    )
    # Nested skip dir should still be ignored
    bad = ws / ".venv" / "lib"
    bad.mkdir(parents=True)
    (bad / "x.py").write_text("1\n", encoding="utf-8")

    tree = workspace_tree_lines(ws)
    assert "KNOWLEDGE.md" in tree
    assert "aiflow-core-architecture.md" in tree
    assert "aiflow-core-architecture.svg" in tree
    assert not any(".venv" in p for p in tree)
