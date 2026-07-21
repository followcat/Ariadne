"""AtelierManager: shared workspace, branch merge/discard."""

from pathlib import Path

from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.knowledge import read_knowledge
from ariadne.atelier.models import SessionStatus, append_transcript, read_transcript


def test_create_list_and_shared_workspace(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    code = tmp_path / "code"
    code.mkdir()
    (code / "app.py").write_text("print('hi')\n", encoding="utf-8")
    proj = mgr.create_project("my-app", from_path=code)
    assert proj.id == "my-app"
    assert (proj.path / "project.json").is_file()
    assert proj.workspace_path == code.resolve()
    assert (proj.knowledge_path).is_file()
    # main auto
    main = mgr.get_or_create_main_session("my-app")
    assert main.id == "main"
    # shared workspace: write via main path visible to branch path
    (proj.workspace_path / "note.txt").write_text("shared\n", encoding="utf-8")
    branch = mgr.create_branch("my-app", "experiment")
    assert branch.id == "branch-experiment"
    assert branch.status == SessionStatus.ACTIVE
    assert (proj.workspace_path / "note.txt").read_text(encoding="utf-8") == "shared\n"
    assert mgr.list_projects()


def test_create_chinese_display_name(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("画画", no_scan=True)
    assert proj.name == "画画"
    assert proj.id.startswith("atelier-")
    # re-list shows display name
    listed = mgr.list_projects()
    assert any(p.name == "画画" and p.id == proj.id for p in listed)


def test_branch_merge_updates_knowledge_discard_does_not(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("p1", no_scan=True)
    before = read_knowledge(proj)
    br = mgr.create_branch("p1", "jwt")
    append_transcript(
        proj,
        br.id,
        {"role": "user", "content": "我们决定使用 JWT 做认证"},
    )
    append_transcript(
        proj,
        br.id,
        {"role": "assistant", "content": "好的，采用 JWT"},
    )
    summary = mgr.merge_branch("p1", "jwt")
    assert "jwt" in summary.lower() or "分支" in summary
    after = read_knowledge(proj)
    # merge appends a short note block only (user trims into 决策与约定)
    assert after != before
    assert "jwt" in after.lower() or "分支合并" in after
    meta = mgr.get_session("p1", "branch-jwt")
    assert meta.status == SessionStatus.MERGED
    # main notified
    main_lines = read_transcript(proj, "main")
    assert any("merged" in str(x.get("content", "")).lower() or "分支" in str(x.get("content", "")) for x in main_lines)

    br2 = mgr.create_branch("p1", "tmp")
    k0 = read_knowledge(proj)
    mgr.discard_branch("p1", "tmp")
    assert read_knowledge(proj) == k0
    assert mgr.get_session("p1", "branch-tmp").status == SessionStatus.DISCARDED
