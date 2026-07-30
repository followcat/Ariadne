"""Red tests for normative 2C memory scope/search guarantees.

These tests intentionally describe the target behavior from
``docs/design/memory-scopes.md`` and ``docs/design/memory-search.md``.  They
should fail until the corresponding implementation gaps are closed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from ariadne.config import Settings, load_settings
from ariadne.errors import AriadneError
from ariadne.memory import Memory
from ariadne.types import TurnResult


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _append_turn(mem: Memory, *, session_id: str, turn_id: str, text: str) -> None:
    mem.transcript.append(
        {
            "role": "user",
            "content": text,
            "turn_id": turn_id,
            "session_id": session_id,
        }
    )
    # Dual-write workspace + user episodic via facade API
    mem.index_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_text=text,
        assistant_text="",
    )


async def _capture_web_turn_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Settings]:
    """Run one HTTP turn per account and capture the Settings sent to compose."""
    web_app = importlib.import_module("ariadne.web.app")
    captured: list[Settings] = []

    class FakeAgent:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings

        async def run(self, _input: str) -> TurnResult:
            return TurnResult(
                turn_id="turn-fake",
                status="completed",
                text="ok",
                session_id=self.settings.session_id,
                model=self.settings.model,
            )

    def fake_compose(settings: Settings) -> FakeAgent:
        captured.append(settings)
        return FakeAgent(settings)

    monkeypatch.setattr(web_app, "compose_agent", fake_compose)
    settings = dataclasses.replace(
        load_settings(workspace=tmp_path / "workspace"),
        data_dir=tmp_path / "serve-data",
    )
    app = web_app.create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for username in ("alice", "bob"):
            response = await client.post(
                "/api/auth/register",
                json={"username": username, "password": "password123"},
            )
            assert response.status_code == 200, response.text
            headers = {"Authorization": f"Bearer {response.json()['token']}"}
            response = await client.put(
                "/api/me/provider",
                json={
                    "base_url": "https://api.example.invalid/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                },
                headers=headers,
            )
            assert response.status_code == 200, response.text
            response = await client.post(
                "/api/turns",
                json={"input": "hello", "session_id": f"session-{username}"},
                headers=headers,
            )
            assert response.status_code == 200, response.text

    return {Path(settings.data_dir).name: settings for settings in captured}


def test_web_turn_binds_registered_account_as_memory_user_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_by_user = _run(_capture_web_turn_settings(tmp_path, monkeypatch))

    assert settings_by_user["alice"].user_id == "alice"
    assert settings_by_user["bob"].user_id == "bob"


def test_web_turn_uses_per_account_user_memory_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_by_user = _run(_capture_web_turn_settings(tmp_path, monkeypatch))

    roots: dict[str, Path] = {}
    for username, settings in settings_by_user.items():
        assert settings.user_memory_dir is not None
        root = Path(settings.user_memory_dir)
        assert root == Path(settings.data_dir) / "memory"
        roots[username] = root
    assert roots["alice"] != roots["bob"]


def test_local_memory_rejects_mismatched_explicit_user_id(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory", user_id="local")

    with pytest.raises(AriadneError) as exc_info:
        mem.build_context(session_id="s1", query="hello", user_id="someone-else")

    assert exc_info.value.error.code == "ARIADNE_CONFIG_INVALID"


def test_workspace_search_honors_before_turn_id_strictly(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    _append_turn(mem, session_id="s1", turn_id="t1", text="older context")
    _append_turn(mem, session_id="s1", turn_id="t2", text="cutoff-only needle")

    result = _run(
        mem.memory_search(
            query="cutoff-only needle",
            session_id="s1",
            scope="workspace",
            mode="fast",
            before_turn_id="t2",
        )
    )

    assert all(hit["turn_id"] != "t2" for hit in result["hits"])


def test_deep_search_does_not_claim_split_without_running_subqueries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    searched_queries: list[str] = []

    async def recording_search(**kwargs: Any) -> list[dict[str, Any]]:
        searched_queries.append(str(kwargs["query"]))
        return [
            {
                "turn_id": "t1",
                "session_id": "s1",
                "kind": "user",
                "score": 0.5,
                "snippet": "alpha and beta",
            }
        ]

    monkeypatch.setattr(mem.semantic, "search_hybrid", recording_search)
    result = _run(
        mem.memory_search(
            query="alpha and beta",
            session_id="s1",
            scope="session",
            mode="deep",
        )
    )

    claimed_split = "split" in str(result.get("notes") or "")
    assert not claimed_split or len(set(searched_queries)) > 1


def test_user_search_includes_grounded_episodic_turns(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    _append_turn(
        mem,
        session_id="old-session",
        turn_id="turn-episodic",
        text="the cobalt migration decision",
    )

    result = _run(
        mem.memory_search(
            query="cobalt migration",
            session_id="new-session",
            scope="user",
            mode="fast",
        )
    )

    assert any(hit["turn_id"] == "turn-episodic" for hit in result["hits"])


def test_memory_search_never_returns_hits_without_turn_provenance(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    mem.apply_curated(
        action="add",
        content="blank provenance preference",
        scope="user",
        session_id="origin-session",
    )

    result = _run(
        mem.memory_search(
            query="blank provenance",
            session_id="query-session",
            scope="user",
            mode="fast",
        )
    )

    for hit in result["hits"]:
        assert hit["turn_id"]
        assert not hit["turn_id"].startswith("curated:")
        assert hit["session_id"]
        assert hit["evidence"]["source"] in {"raw", "summary", "chunk", "curated"}


def test_memory_search_limit_over_hard_cap_fastfails(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")

    with pytest.raises(AriadneError) as exc_info:
        _run(
            mem.memory_search(
                query="needle",
                session_id="s1",
                scope="session",
                mode="fast",
                limit=33,
            )
        )

    assert exc_info.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"


def test_fast_lexical_search_supports_chinese_text(tmp_path: Path) -> None:
    mem = Memory.local(path=tmp_path / "memory")
    mem.hybrid_semantic = False
    _append_turn(
        mem,
        session_id="s1",
        turn_id="turn-cn",
        text="我们讨论了登录失败问题",
    )

    result = _run(
        mem.memory_search(
            query="登录失败",
            session_id="s1",
            scope="session",
            mode="fast",
        )
    )

    assert any(hit["turn_id"] == "turn-cn" for hit in result["hits"])
