import asyncio
from pathlib import Path

from ariadne.sandbox.active import ActiveSessionManager
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.sandbox.port import SandboxExecRequest


def test_active_session_reuses_and_clears(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    backend = LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data")
    mgr = ActiveSessionManager(backend, idle_ttl_seconds=600, max_ttl_seconds=3600)

    async def run() -> None:
        s1 = await mgr.get_or_start(session_id="chat1")
        await s1.exec(SandboxExecRequest(cmd="printf 'x' > f.txt", cwd="/session"))
        s2 = await mgr.get_or_start(session_id="chat1")
        assert s1 is s2
        out = await s2.exec(SandboxExecRequest(cmd="cat f.txt", cwd="/session"))
        assert out.stdout.strip() == "x"
        ok = await mgr.clear_session_files("chat1")
        assert ok is True
        out2 = await s2.exec(SandboxExecRequest(cmd="ls -A", cwd="/session"))
        assert out2.stdout.strip() == ""
        st = mgr.status()
        assert st["count"] == 1
        await mgr.close("chat1", reason="test")
        assert mgr.status()["count"] == 0

    asyncio.run(run())


def test_active_session_ttl_reap(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    backend = LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data")
    mgr = ActiveSessionManager(backend, idle_ttl_seconds=0.01, max_ttl_seconds=3600)

    async def run() -> None:
        await mgr.get_or_start(session_id="old")
        await asyncio.sleep(0.05)
        closed = await mgr.reap()
        assert "old" in closed
        assert mgr.status()["count"] == 0

    asyncio.run(run())
