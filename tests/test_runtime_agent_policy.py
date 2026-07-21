"""In-process RuntimeAgent + CommandPolicy + EgressPolicy."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.sandbox.policy import CommandPolicy, EgressPolicy
from ariadne.sandbox.runtime_agent import RuntimeAgent


def test_command_policy_denies_rm_root() -> None:
    p = CommandPolicy()
    ok, reason = p.is_allowed("rm -rf /")
    assert not ok
    assert "denied" in reason


def test_command_policy_redact_secrets() -> None:
    p = CommandPolicy()
    text = p.redact("token sk-abcdefghijklmnopqrstuvwxyz0123456789 end")
    assert "sk-***" in text
    assert "sk-abcdef" not in text


def test_runtime_agent_shell_allow_and_deny(tmp_path: Path) -> None:
    backend = LocalWorkdirSandbox(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    (tmp_path / "ws").mkdir()
    audit = tmp_path / "audit.jsonl"
    policy = CommandPolicy(audit_path=audit)
    agent = RuntimeAgent(command_policy=policy)

    async def run() -> None:
        session = await backend.start(scope_key="t1")
        agent.bind(session)
        try:
            out = await agent.execute_shell("echo hello", cwd="/workspace")
            assert out["exit_code"] == 0
            assert "hello" in out["stdout"]
            with pytest.raises(AriadneError) as ei:
                await agent.execute_shell("rm -rf /")
            assert ei.value.error.code == "ARIADNE_TOOL_DENIED"
        finally:
            await session.close(reason="test")

    asyncio.run(run())
    assert audit.is_file()
    body = audit.read_text(encoding="utf-8")
    assert "shell_ok" in body
    assert "shell_deny" in body


def test_egress_policy_allowlist() -> None:
    p = EgressPolicy(allowed_hosts=("example.com", "api.github.com"), default_allow=False)
    assert p.check_url("https://example.com/x")[0]
    assert p.check_url("https://api.github.com/repos")[0]
    ok, reason = p.check_url("https://evil.example.org")
    # evil.example.org ends with .example.com? ends with .example.com - "evil.example.org" does not end with .example.com
    assert not ok
    assert not p.check_url("https://attacker.com")[0]
