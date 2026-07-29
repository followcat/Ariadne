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


def test_branch_settings_mount_main_readonly(tmp_path: Path) -> None:
    """Branch bind sets main_readonly_workspace to main tree (live RO)."""
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("ro-main", no_scan=True)
    (proj.workspace_path / "main_live.txt").write_text("LIVE\n", encoding="utf-8")
    br = mgr.create_branch(proj.id, "exp")
    # mutate main after branch create
    (proj.workspace_path / "main_live.txt").write_text("LIVE2\n", encoding="utf-8")
    base = load_settings(workspace=tmp_path / "other", force_workspace=True, sandbox="local")
    s_br = settings_for_atelier(proj, br, base)
    assert s_br.main_readonly_workspace == proj.workspace_path
    assert s_br.workspace == proj.branch_workspace_path("exp")
    # local sandbox can read live main via /main-readonly
    from ariadne.sandbox.local import LocalWorkdirSandbox

    backend = LocalWorkdirSandbox(
        workspace=s_br.workspace,
        data_dir=tmp_path / "data",
        main_readonly=s_br.main_readonly_workspace,
    )

    async def run() -> None:
        session = await backend.start(scope_key="br-ro")
        data = await session.read_file("/main-readonly/main_live.txt")
        assert data == b"LIVE2\n"
        # write to main-readonly must fail
        import pytest
        from ariadne.errors import AriadneError

        with pytest.raises(AriadneError):
            await session.write_file("/main-readonly/x.txt", b"nope")
        await session.write_file("/workspace/side.txt", b"ok\n")
        assert (proj.branch_workspace_path("exp") / "side.txt").read_text(
            encoding="utf-8"
        ) == "ok\n"
        await session.close(reason="test")

    import asyncio

    asyncio.run(run())


def test_branch_workspace_has_knowledge_copy(tmp_path: Path) -> None:
    """Branch /workspace/KNOWLEDGE.md is a readable copy of the root 便签."""
    from ariadne.atelier.knowledge import read_knowledge, write_knowledge
    from ariadne.atelier.runner import settings_for_atelier

    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("kb-br", no_scan=True)
    write_knowledge(
        proj,
        "# kb-br\n\n## 本坊怎么运作\n- 旁支可读便签\n\n## 关键路径\n- `/workspace`\n",
        session_id="t",
    )
    br = mgr.create_branch(proj.id, "exp")
    bws = proj.branch_workspace_path("exp")
    kn = bws / "KNOWLEDGE.md"
    assert kn.is_file(), "branch seed should include KNOWLEDGE.md"
    assert "旁支可读便签" in kn.read_text(encoding="utf-8")

    base = load_settings(workspace=tmp_path / "other", force_workspace=True, sandbox="null")
    settings_for_atelier(proj, br, base)
    # After main updates brief, next bind refreshes branch copy
    write_knowledge(
        proj,
        read_knowledge(proj) + "\n- 新约定：纯本地\n",
        session_id="t2",
    )
    settings_for_atelier(proj, br, base)
    assert "纯本地" in kn.read_text(encoding="utf-8")


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


def test_atelier_compose_keeps_account_plugins(tmp_path: Path) -> None:
    """Web account plugins must remain after atelier rebinds session data_dir."""
    import asyncio
    import dataclasses

    from ariadne.host.compose import compose_agent
    from ariadne.plugins import PluginStore

    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("plug-lab", no_scan=True)
    main = mgr.get_or_create_main_session(proj.id)
    br = mgr.create_branch(proj.id, "exp")

    account = tmp_path / "web-user"
    account.mkdir()
    PluginStore(account / "plugins.json").enable(
        "gitlab", {"url": "http://gitlab.test", "token": "tok"}
    )
    # No plugins under atelier data_dir — only account store.
    base = load_settings(
        workspace=tmp_path / "other",
        force_workspace=True,
        sandbox="null",
    )
    base = dataclasses.replace(
        base,
        data_dir=account,
        plugins_dir=account,
        merge_home_plugins=False,
        base_url="http://example.invalid",
        api_key="k",
        model="m",
    )
    bound = settings_for_atelier(proj, br, base)
    assert bound.data_dir != account
    assert bound.plugins_dir == account
    assert not (Path(bound.data_dir) / "plugins.json").is_file()

    async def tool_names(agent) -> set[str]:
        return {t["name"] for t in await agent.list_tools()}

    names = asyncio.run(tool_names(compose_agent(bound)))
    assert "gitlab_request" in names

    bound_main = settings_for_atelier(proj, main, base)
    names_main = asyncio.run(tool_names(compose_agent(bound_main)))
    assert "gitlab_request" in names_main


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
