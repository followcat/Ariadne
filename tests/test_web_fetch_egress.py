"""web_fetch tool respects egress policy (host-side)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.memory import Memory
from ariadne.sandbox.policy import EgressPolicy
from ariadne.sandbox.runtime_agent import RuntimeAgent
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import ToolContext, build_default_registry


def test_web_fetch_denied_by_default() -> None:
    memory = Memory.local(path=Path("/tmp/not-used-mem"))  # will use tmp via Memory.local
    # use real tmp
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        memory = Memory.local(path=Path(td) / "m")
        skills = SkillStore.from_dirs([], strict=False, user_root=Path(td) / "skills")
        reg = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
        agent = RuntimeAgent(egress_policy=EgressPolicy(default_allow=False, allowed_hosts=()))
        ctx = ToolContext(
            session_id="s",
            turn_id="t",
            sandbox=None,
            memory=memory,
            skills=skills,
            runtime_agent=agent,
        )

        async def run() -> None:
            with pytest.raises(AriadneError) as ei:
                await reg.invoke(
                    "web_fetch",
                    {"url": "https://example.com"},
                    ctx,
                )
            assert ei.value.error.code == "ARIADNE_TOOL_DENIED"

        asyncio.run(run())


def test_web_fetch_allowlist_uses_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile

    class _Resp:
        status_code = 200
        url = "https://example.com/ok"
        headers = {"content-type": "text/plain"}
        text = "hello-from-mock"

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def request(self, method: str, url: str) -> _Resp:
            assert method == "GET"
            assert "example.com" in url
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    with tempfile.TemporaryDirectory() as td:
        memory = Memory.local(path=Path(td) / "m")
        skills = SkillStore.from_dirs([], strict=False, user_root=Path(td) / "skills")
        reg = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)
        agent = RuntimeAgent(
            egress_policy=EgressPolicy(allowed_hosts=("example.com",), default_allow=False)
        )
        ctx = ToolContext(
            session_id="s",
            turn_id="t",
            sandbox=None,
            memory=memory,
            skills=skills,
            runtime_agent=agent,
        )

        async def run() -> dict:
            return await reg.invoke(
                "web_fetch",
                {"url": "https://example.com/ok"},
                ctx,
            )

        out = asyncio.run(run())
        assert out["status_code"] == 200
        assert "hello-from-mock" in out["body"]
