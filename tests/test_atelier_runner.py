from pathlib import Path

from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.runner import build_system_prompt, maybe_update_knowledge_after_turn, settings_for_atelier
from ariadne.config import load_settings


def test_system_prompt_includes_knowledge_and_branch(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("r1", no_scan=True)
    main = mgr.get_or_create_main_session("r1")
    prompt = build_system_prompt(proj, main)
    assert "小本本" in prompt or "KNOWLEDGE" in prompt or "备忘" in prompt or "便签" in prompt
    assert "改文件" in prompt or "保存" in prompt or "写" in prompt
    assert proj.name in prompt
    br = mgr.create_branch("r1", "exp")
    bprompt = build_system_prompt(proj, br)
    assert "旁支" in bprompt or "分支" in bprompt
    # Default atelier has no architecture 便签 → must NOT hard-require architecture.md
    assert "不要默认产出" in bprompt or "未强制架构" in bprompt
    assert "旁支交付" in bprompt or "输出规范" in bprompt
    assert "/main-readonly" in bprompt
    # Architecture deliverables only when THIS atelier's 便签 asks for them
    assert "architecture.md" not in bprompt or "不要默认" in bprompt


def test_branch_prompt_includes_main_tree_and_output_spec(tmp_path: Path) -> None:
    """Branch inject shows main-readonly tree + 便签 输出规范 excerpt."""
    from ariadne.atelier.knowledge import write_knowledge

    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("arch1", no_scan=True)
    (proj.workspace_path / "sample-note.md").write_text("# main only\n", encoding="utf-8")
    write_knowledge(
        proj,
        "# arch1\n\n## 输出规范\n"
        "- 架构图 `.svg`\n"
        "- 架构描述 `.md`\n"
        "- 用 `![x](/workspace/x.svg)` 展示\n",
    )
    br = mgr.create_branch("arch1", "aiflow-core")
    # Seed a stale demo file on the branch to ensure warning appears
    bws = proj.session_workspace(br)
    (bws / "cohersoup-architecture.md").write_text("old demo\n", encoding="utf-8")
    bprompt = build_system_prompt(proj, br)
    assert "sample-note.md" in bprompt  # main tree under main-readonly section
    assert "输出规范" in bprompt
    assert ".svg" in bprompt
    assert "cohersoup" in bprompt.lower()  # warn about stale demo


def test_drawing_atelier_does_not_force_architecture(tmp_path: Path) -> None:
    """Non-architecture 便签 must not inject global architecture SVG+MD policy."""
    from ariadne.atelier.knowledge import write_knowledge

    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("paint-shop", no_scan=True)
    write_knowledge(
        proj,
        "# 画画\n\n## 本坊怎么运作\n- 蜡笔画与 PNG 输出\n\n## 约定\n- 出图写 PNG\n",
    )
    br = mgr.create_branch("paint-shop", "jurassic")
    bprompt = build_system_prompt(proj, br)
    assert "不要默认产出" in bprompt or "未强制架构" in bprompt
    assert "architecture.md" not in bprompt or "不要默认" in bprompt
    # Architecture atelier still gets its own 便签 excerpt
    proj2 = mgr.create_project("arch-shop", no_scan=True)
    write_knowledge(
        proj2,
        "# 架构坊\n\n## 输出规范\n- 架构描述 architecture.md\n- 架构图 .svg\n",
    )
    br2 = mgr.create_branch("arch-shop", "core")
    p2 = build_system_prompt(proj2, br2)
    assert "architecture.md" in p2 or "架构描述" in p2
    assert "输出规范" in p2


def test_build_prompt_includes_workspace_tree(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("tree1", no_scan=True)
    (proj.workspace_path / "index.html").write_text("<html></html>\n", encoding="utf-8")
    (proj.workspace_path / "crayon.js").write_text("// js\n", encoding="utf-8")
    main = mgr.get_or_create_main_session("tree1")
    prompt = build_system_prompt(proj, main)
    assert "index.html" in prompt
    assert "crayon.js" in prompt
    assert "文件夹" in prompt or "index.html" in prompt


def test_main_auto_append_agreements_branch_skips(tmp_path: Path) -> None:
    """Default on for main when 约定 present; branch never writes; noise noop."""
    from ariadne.atelier.knowledge import read_knowledge

    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("r2", no_scan=True)
    main = mgr.get_or_create_main_session("r2")
    br = mgr.create_branch("r2", "b1")
    # Branch: never write even with agreement language
    assert not maybe_update_knowledge_after_turn(
        proj, br, user_text="我们决定使用 X", assistant_text="ok"
    )
    # Main noise: no write
    before = read_knowledge(proj)
    assert not maybe_update_knowledge_after_turn(
        proj, main, user_text="哈哈好的", assistant_text="嗯嗯"
    )
    assert read_knowledge(proj) == before
    # Main agreement: small-step write (default enabled)
    assert maybe_update_knowledge_after_turn(
        proj,
        main,
        user_text="我们决定使用 X 方案",
        assistant_text="记录了",
    )
    assert "X 方案" in read_knowledge(proj) or "使用 X" in read_knowledge(proj)
    # Explicit disable still works
    mid = read_knowledge(proj)
    assert not maybe_update_knowledge_after_turn(
        proj,
        main,
        user_text="我们决定改用 Y 方案",
        assistant_text="好",
        enabled=False,
    )
    assert read_knowledge(proj) == mid


def test_settings_for_atelier_binds_workspace(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("r3")
    main = mgr.get_or_create_main_session("r3")
    base = load_settings(workspace=tmp_path / "other", force_workspace=True, sandbox="local")
    s = settings_for_atelier(proj, main, base)
    assert s.workspace == proj.workspace_path
    assert s.session_id == "aw-r3-main"
    assert "atelier-atelier-" not in s.session_id
    assert s.data_dir == proj.data_dir
    assert s.max_tokens >= 16384
