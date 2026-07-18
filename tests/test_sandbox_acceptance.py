"""Acceptance scenarios from docs/design/sandbox-v1.md §11."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.sandbox.null import NullSandbox
from ariadne.sandbox.port import SandboxExecRequest


def _backend(tmp_path: Path) -> LocalWorkdirSandbox:
    workspace = tmp_path / "proj"
    workspace.mkdir(exist_ok=True)
    return LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data")


def test_scenario_1_session_scratch_gone_after_close(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s1-turn1")
        first = await session.exec(
            SandboxExecRequest(cmd="printf 'scratch\\n' > note.txt", cwd="/session")
        )
        assert first.exit_code == 0
        # second exec in the same scope still sees the file
        second = await session.exec(SandboxExecRequest(cmd="cat note.txt", cwd="/session"))
        assert second.exit_code == 0
        assert "scratch" in second.stdout
        session_dir = backend.data_dir / "sandbox" / "s1-turn1" / "session"
        assert session_dir.is_dir()
        await session.close(reason="turn_finished")
        assert not session_dir.exists(), "per_turn close must remove /session scratch"

    asyncio.run(run())


def test_scenario_2_workspace_survives_across_turns(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        turn1 = await backend.start(scope_key="s1-turn1")
        result = await turn1.exec(
            SandboxExecRequest(cmd="printf 'durable\\n' > keep.txt", cwd="/workspace")
        )
        assert result.exit_code == 0
        await turn1.close(reason="turn_finished")

        turn2 = await backend.start(scope_key="s1-turn2")
        check = await turn2.exec(SandboxExecRequest(cmd="cat keep.txt", cwd="/workspace"))
        assert check.exit_code == 0
        assert "durable" in check.stdout
        await turn2.close(reason="turn_finished")

    asyncio.run(run())


def test_scenario_3_timeout_is_structured(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s-timeout")
        result = await session.exec(
            SandboxExecRequest(cmd="sleep 5", cwd="/session", timeout_seconds=0.2)
        )
        assert result.timed_out is True
        assert "timed out" in result.stderr
        await session.close(reason="test")

    asyncio.run(run())


def test_scenario_4_huge_output_truncated_with_marker(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s-huge")
        result = await session.exec(
            SandboxExecRequest(cmd="seq 1 100000", cwd="/session", max_stdout_bytes=10_000)
        )
        assert result.exit_code == 0, "exit_code must be preserved"
        assert result.truncated or result.compressed
        assert "[ariadne:" in result.stdout
        assert len(result.stdout.encode()) < 100_000
        await session.close(reason="test")

    asyncio.run(run())


def test_scenario_5_path_escape_rejected(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s-escape")
        with pytest.raises(AriadneError):
            await session.read_file("/session/../../etc/passwd")
        with pytest.raises(AriadneError):
            await session.read_file("/etc/passwd")
        with pytest.raises(AriadneError):
            await session.write_file("/workspace/../escape.txt", b"nope")
        await session.close(reason="test")

    asyncio.run(run())


def test_scenario_6_null_sandbox_fastfails() -> None:
    backend = NullSandbox()

    async def run() -> None:
        session = await backend.start(scope_key="s-null")
        for call in (
            session.exec(SandboxExecRequest(cmd="echo hi")),
            session.read_file("/workspace/x.txt"),
            session.list_dir("/workspace"),
        ):
            with pytest.raises(AriadneError) as excinfo:
                await call
            assert excinfo.value.error.code == "ARIADNE_SANDBOX_DISABLED"
        await session.close(reason="test")

    asyncio.run(run())


def test_file_api_roundtrip(tmp_path: Path) -> None:
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s-files")
        await session.write_file("/session/sub/data.bin", b"\x00\x01binary")
        assert await session.read_file("/session/sub/data.bin") == b"\x00\x01binary"
        await session.write_file("/workspace/notes.md", "# notes\n".encode())
        listing = await session.list_dir("/session/sub")
        assert listing == ["data.bin"]
        root_listing = await session.list_dir("/workspace")
        assert "notes.md" in root_listing
        with pytest.raises(AriadneError):
            await session.read_file("/session/missing.txt")
        await session.close(reason="test")

    asyncio.run(run())


def test_env_allowlist_no_host_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("MY_CUSTOM_VAR", "custom-value")
    backend = _backend(tmp_path)

    async def run() -> None:
        session = await backend.start(scope_key="s-env")
        result = await session.exec(
            SandboxExecRequest(cmd="echo key=${OPENAI_API_KEY:-unset} custom=${MY_CUSTOM_VAR:-unset} path_ok=${PATH:+yes}")
        )
        assert result.exit_code == 0
        assert "key=unset" in result.stdout, "host secrets must not leak into sandbox"
        assert "custom=unset" in result.stdout, "allowlist blocks arbitrary host vars"
        assert "path_ok=yes" in result.stdout, "PATH is on the default allowlist"
        # explicit req.env still passes through
        injected = await session.exec(
            SandboxExecRequest(cmd="echo v=$INJECTED", env={"INJECTED": "42"})
        )
        assert "v=42" in injected.stdout
        await session.close(reason="test")

    asyncio.run(run())
