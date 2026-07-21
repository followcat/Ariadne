from pathlib import Path

from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.runner import build_system_prompt, maybe_update_knowledge_after_turn, settings_for_atelier
from ariadne.config import load_settings


def test_system_prompt_includes_knowledge_and_branch(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("r1", no_scan=True)
    main = mgr.get_or_create_main_session("r1")
    prompt = build_system_prompt(proj, main)
    assert "小本本" in prompt or "KNOWLEDGE" in prompt or "备忘" in prompt
    assert "改文件" in prompt or "保存" in prompt or "写" in prompt
    assert proj.name in prompt
    br = mgr.create_branch("r1", "exp")
    bprompt = build_system_prompt(proj, br)
    assert "旁支" in bprompt or "分支" in bprompt


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


def test_auto_extract_off_by_default(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "a")
    proj = mgr.create_project("r2", no_scan=True)
    main = mgr.get_or_create_main_session("r2")
    br = mgr.create_branch("r2", "b1")
    # default: never rewrite KNOWLEDGE from dialogue
    assert not maybe_update_knowledge_after_turn(
        proj, br, user_text="我们决定使用 X", assistant_text="ok"
    )
    assert not maybe_update_knowledge_after_turn(
        proj, main, user_text="我们决定使用 X 方案", assistant_text="记录了"
    )
    # opt-in path still works for tests / power users
    assert maybe_update_knowledge_after_turn(
        proj,
        main,
        user_text="我们决定使用 X 方案",
        assistant_text="记录了",
        enabled=True,
    )


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
