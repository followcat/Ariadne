"""AtelierManager: branch isolation (workspace + data), merge/discard."""

from pathlib import Path

from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.knowledge import read_knowledge
from ariadne.atelier.models import SessionStatus, append_transcript, read_transcript
from ariadne.atelier.runner import settings_for_atelier
from ariadne.config import load_settings


def test_create_list_and_main_workspace(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    code = tmp_path / "code"
    code.mkdir()
    (code / "app.py").write_text("print('hi')\n", encoding="utf-8")
    proj = mgr.create_project("my-app", from_path=code)
    assert proj.id == "my-app"
    assert (proj.path / "project.json").is_file()
    assert proj.workspace_path == code.resolve()
    assert (proj.knowledge_path).is_file()
    main = mgr.get_or_create_main_session("my-app")
    assert main.id == "main"
    (proj.workspace_path / "note.txt").write_text("shared\n", encoding="utf-8")
    assert mgr.list_projects()


def test_branch_has_isolated_workspace_and_data(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("iso", no_scan=True)
    (proj.workspace_path / "main_only.txt").write_text("MAIN\n", encoding="utf-8")
    br = mgr.create_branch("iso", "exp")
    bws = proj.branch_workspace_path("exp")
    assert bws.is_dir()
    assert (bws / "main_only.txt").read_text(encoding="utf-8") == "MAIN\n"
    # Branch write does not touch main
    (bws / "branch_only.txt").write_text("BRANCH\n", encoding="utf-8")
    assert not (proj.workspace_path / "branch_only.txt").exists()
    (bws / "main_only.txt").write_text("MUTATED\n", encoding="utf-8")
    assert (proj.workspace_path / "main_only.txt").read_text(encoding="utf-8") == "MAIN\n"
    # session paths
    assert proj.session_workspace(br) == bws
    main = mgr.get_or_create_main_session("iso")
    assert proj.session_workspace(main) == proj.workspace_path
    assert "scopes" in str(proj.session_data_dir(br))
    assert proj.session_data_dir(main) == proj.data_dir
    # settings bind isolation
    base = load_settings(workspace=tmp_path / "other", force_workspace=True, sandbox="local")
    s_br = settings_for_atelier(proj, br, base)
    s_main = settings_for_atelier(proj, main, base)
    assert s_br.workspace == bws
    assert s_main.workspace == proj.workspace_path
    assert s_br.data_dir != s_main.data_dir


def test_create_chinese_display_name(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("画画", no_scan=True)
    assert proj.name == "画画"
    assert proj.id.startswith("atelier-")
    listed = mgr.list_projects()
    assert any(p.name == "画画" and p.id == proj.id for p in listed)


def test_chinese_branch_name(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("画室", no_scan=True)
    br = mgr.create_branch(proj.id, "V字仇杀队")
    assert br.title == "V字仇杀队"
    assert br.branch_name
    assert br.branch_name.startswith("br-")
    assert br.id == f"branch-{br.branch_name}"
    # merge / discard resolve by Chinese title
    summary = mgr.merge_branch(proj.id, "V字仇杀队")
    assert summary
    assert mgr.get_session(proj.id, br.id).status == SessionStatus.MERGED

    br2 = mgr.create_branch(proj.id, "月光小鸟")
    mgr.discard_branch(proj.id, "月光小鸟")
    assert mgr.get_session(proj.id, br2.id).status == SessionStatus.DISCARDED


def test_branch_merge_does_not_touch_main_knowledge_or_workspace(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("p1", no_scan=True)
    (proj.workspace_path / "keep.txt").write_text("K\n", encoding="utf-8")
    before_k = read_knowledge(proj)
    br = mgr.create_branch("p1", "jwt")
    bws = proj.branch_workspace_path("jwt")
    (bws / "secret.txt").write_text("from-branch\n", encoding="utf-8")
    append_transcript(
        proj,
        br.id,
        {"role": "user", "content": "我们决定使用 JWT 做认证"},
    )
    summary = mgr.merge_branch("p1", "jwt")
    assert "jwt" in summary.lower() or "分支" in summary
    # Main knowledge & workspace untouched
    assert read_knowledge(proj) == before_k
    assert (proj.workspace_path / "keep.txt").read_text(encoding="utf-8") == "K\n"
    assert not (proj.workspace_path / "secret.txt").exists()
    meta = mgr.get_session("p1", "branch-jwt")
    assert meta.status == SessionStatus.MERGED
    # No main transcript pollution required
    main_lines = read_transcript(proj, "main")
    assert not any("jwt" in str(x.get("content", "")).lower() for x in main_lines)

    br2 = mgr.create_branch("p1", "tmp")
    bws2 = proj.branch_workspace_path("tmp")
    (bws2 / "x.txt").write_text("x\n", encoding="utf-8")
    k0 = read_knowledge(proj)
    mgr.discard_branch("p1", "tmp")
    assert read_knowledge(proj) == k0
    assert not bws2.exists()
    assert mgr.get_session("p1", "branch-tmp").status == SessionStatus.DISCARDED
