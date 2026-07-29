"""File tools over the sandbox session FS API (write/read/edit + diffs)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.tools.filetools import sandbox_edit_file, sandbox_read_file, sandbox_write_file
from ariadne.tools.registry import ToolContext, ToolRegistry


def _ctx(session) -> ToolContext:
    return ToolContext(session_id="s1", turn_id="t1", sandbox=session)


def test_write_read_edit_roundtrip(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    backend = LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data")

    async def run() -> None:
        session = await backend.start(scope_key="ft")
        ctx = _ctx(session)

        written = await sandbox_write_file(
            {"path": "/session/notes.md", "content": "line one\nline two\n"}, ctx
        )
        assert written["created"] is True
        assert "+line one" in written["diff"]

        read = await sandbox_read_file({"path": "/session/notes.md"}, ctx)
        assert read["content"] == "line one\nline two\n"

        edited = await sandbox_edit_file(
            {
                "path": "/session/notes.md",
                "old_string": "line two",
                "new_string": "line 2",
            },
            ctx,
        )
        assert "-line two" in edited["diff"] and "+line 2" in edited["diff"]
        assert (await sandbox_read_file({"path": "/session/notes.md"}, ctx))[
            "content"
        ] == "line one\nline 2\n"

        rewritten = await sandbox_write_file(
            {"path": "/session/notes.md", "content": "fresh\n"}, ctx
        )
        assert rewritten["created"] is False
        assert "-line one" in rewritten["diff"]
        await session.close(reason="test")

    asyncio.run(run())


def test_edit_requires_unique_match(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    backend = LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data")

    async def run() -> None:
        session = await backend.start(scope_key="ft2")
        ctx = _ctx(session)
        with pytest.raises(AriadneError) as excinfo:
            await sandbox_edit_file(
                {"path": "dup.txt", "old_string": "same", "new_string": "different"}, ctx
            )
        assert excinfo.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"
        assert excinfo.value.error.details["occurrences"] == 2
        with pytest.raises(AriadneError) as excinfo2:
            await sandbox_edit_file(
                {"path": "dup.txt", "old_string": "nope", "new_string": "x"}, ctx
            )
        assert excinfo2.value.error.details["occurrences"] == 0
        await session.close(reason="test")

    asyncio.run(run())


def test_write_new_file_tolerates_docker_no_such_file_message() -> None:
    """Pre-write read must treat Docker 'No such file' as empty, not fail."""
    from ariadne.errors import app_error
    from ariadne.tools.filetools import _is_missing_file_error, _read_text

    assert _is_missing_file_error(
        "read_file failed: cat: /workspace/KNOWLEDGE.md: No such file or directory"
    )

    class FakeSandbox:
        async def read_file(self, path: str) -> bytes:
            raise AriadneError(
                app_error(
                    "ARIADNE_SANDBOX_EXEC_FAILED",
                    f"read_file failed: cat: {path}: No such file or directory",
                    path=path,
                )
            )

        async def write_file(self, path: str, data: bytes) -> None:
            self.last = (path, data)

    async def run() -> None:
        sb = FakeSandbox()
        assert await _read_text(sb, "/workspace/KNOWLEDGE.md") == ""
        ctx = _ctx(sb)
        out = await sandbox_write_file(
            {"path": "/workspace/KNOWLEDGE.md", "content": "# note\n"},
            ctx,
        )
        assert out["created"] is True
        assert sb.last[0] == "/workspace/KNOWLEDGE.md"

    asyncio.run(run())


def test_file_tools_registered_and_sandboxless_fastfail() -> None:
    registry = ToolRegistry.builtins(include_sandbox=True)
    for name in ("sandbox_read_file", "sandbox_write_file", "sandbox_edit_file"):
        assert name in registry.tools
    no_sandbox = ToolRegistry.builtins(include_sandbox=False)
    for name in ("sandbox_exec", "sandbox_read_file", "sandbox_write_file", "sandbox_edit_file"):
        assert name not in no_sandbox.tools

    ctx = ToolContext(session_id="s", turn_id="t", sandbox=None)

    async def run() -> None:
        with pytest.raises(AriadneError) as excinfo:
            await sandbox_read_file({"path": "/workspace/x"}, ctx)
        assert excinfo.value.error.code == "ARIADNE_SANDBOX_DISABLED"

    asyncio.run(run())
