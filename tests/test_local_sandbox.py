import asyncio
from pathlib import Path

from ariadne.sandbox.compress import compress_observation
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.sandbox.port import SandboxExecRequest


def test_local_sandbox_exec_and_file(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    data = tmp_path / "data"
    backend = LocalWorkdirSandbox(workspace=workspace, data_dir=data)

    async def run() -> None:
        session = await backend.start(scope_key="t1")
        result = await session.exec(
            SandboxExecRequest(cmd="printf 'hello\\n' > hi.txt && cat hi.txt", cwd="/workspace")
        )
        assert result.exit_code == 0
        assert "hello" in result.stdout
        assert (workspace / "hi.txt").read_text() == "hello\n"
        scratch = await session.exec(
            SandboxExecRequest(cmd="printf 'tmp\\n' > a.txt && cat a.txt", cwd="/session")
        )
        assert scratch.exit_code == 0
        assert "tmp" in scratch.stdout
        await session.close(reason="test")

    asyncio.run(run())


def test_compress_log_dedupe() -> None:
    lines = ["INFO start"] + ["INFO heartbeat ok"] * 40 + ["INFO done"]
    text = "\n".join(lines) + "\n"
    result = compress_observation(stdout=text, stderr="", max_stdout_bytes=10_000, strategy="log_dedupe")
    assert result.compressed
    assert "repeated" in result.stdout or result.stdout.count("heartbeat") < 40


def test_compress_head_tail() -> None:
    text = "A" * 5000
    result = compress_observation(
        stdout=text, stderr="", max_stdout_bytes=200, strategy="head_tail"
    )
    assert result.truncated
    assert "truncated" in result.stdout.lower() or "…" in result.stdout or "..." in result.stdout
