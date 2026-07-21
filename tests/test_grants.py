"""Persistent approval grants survive reload (real GrantStore + approval hook)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.cli.approval import WRITE_TOOLS, make_approval_hook
from ariadne.cli.grants import GrantStore, fingerprint
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def test_grant_store_persists_across_reload(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    store = GrantStore(path=path, default_ttl_seconds=3600)
    g = store.create_pending(name="sandbox_exec", args={"cmd": "echo hi"}, session_id="s1")
    gid = g["id"]
    assert g["status"] == "pending"
    store.approve(gid)

    # Reload from disk
    store2 = GrantStore(path=path)
    loaded = store2.get(gid)
    assert loaded is not None
    assert loaded["status"] == "approved"
    assert loaded["fingerprint"] == fingerprint("sandbox_exec", {"cmd": "echo hi"})

    usable = store2.find_usable("sandbox_exec", {"cmd": "echo hi"})
    assert usable is not None
    store2.mark_executed(gid)
    store3 = GrantStore(path=path)
    assert store3.get(gid)["status"] == "executed"


def test_grant_expire_due(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    store = GrantStore(path=path, default_ttl_seconds=1)
    g = store.create_pending(name="sandbox_write_file", args={"path": "/workspace/a"})
    # Force expiry
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[0]["expires_at"] = 1.0
    path.write_text(json.dumps(rows), encoding="utf-8")
    n = store.expire_due(now=100.0)
    assert n == 1
    assert store.get(g["id"])["status"] == "expired"
    assert store.find_usable("sandbox_write_file", {"path": "/workspace/a"}, now=100.0) is None


def test_on_request_hook_reuses_approved_grant(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    store = GrantStore(path=path)
    g = store.create_pending(name="sandbox_exec", args={"cmd": "echo reused"})
    store.approve(g["id"])

    asks: list[str] = []

    def confirm(q: str) -> bool:
        asks.append(q)
        return False  # would deny if asked

    hook = make_approval_hook(
        "on-request", confirm=confirm, grant_store=store, session_id="s"
    )
    assert hook is not None
    assert hook("sandbox_exec", {"cmd": "echo reused"}) is True
    assert asks == [], "should not re-prompt when grant already approved"
    assert store.get(g["id"])["status"] == "executed"


def test_hook_approve_then_reload_reuses_without_reprompt(tmp_path: Path) -> None:
    """Honest path: first allow via hook (confirm=True), new GrantStore, confirm=False.

    Must still allow same fingerprint without re-prompt — proves approve is not
    stuck in a non-reusable state after the first decision.
    """
    path = tmp_path / "grants.json"
    args = {"cmd": "echo once"}
    store1 = GrantStore(path=path)
    asks1: list[str] = []

    def confirm_yes(q: str) -> bool:
        asks1.append(q)
        return True

    hook1 = make_approval_hook(
        "on-request", confirm=confirm_yes, grant_store=store1, session_id="s"
    )
    assert hook1 is not None
    assert hook1("sandbox_exec", args) is True
    assert len(asks1) == 1
    # After first allow, grant must be reusable (approved or executed, not only pending)
    rows = store1.list()
    assert len(rows) == 1
    assert rows[0]["status"] in {"approved", "executed"}

    # Simulate process restart: new store + confirm that would deny if asked
    store2 = GrantStore(path=path)
    asks2: list[str] = []

    def confirm_no(q: str) -> bool:
        asks2.append(q)
        return False

    hook2 = make_approval_hook(
        "on-request", confirm=confirm_no, grant_store=store2, session_id="s"
    )
    assert hook2 is not None
    assert hook2("sandbox_exec", args) is True, "must reuse durable grant after reload"
    assert asks2 == [], "must not re-prompt after reload for same fingerprint"


def test_on_request_hook_records_deny(tmp_path: Path) -> None:
    path = tmp_path / "grants.json"
    store = GrantStore(path=path)
    hook = make_approval_hook(
        "on-request",
        confirm=lambda _q: False,
        grant_store=store,
        session_id="s",
    )
    assert hook is not None
    assert hook("sandbox_exec", {"cmd": "rm -rf /"}) is False
    pending = store.list()
    assert len(pending) == 1
    assert pending[0]["status"] == "denied"


def test_approved_grant_allows_tool_after_store_reload(tmp_path: Path) -> None:
    """End-to-end: first turn confirms via hook; second process-like store runs tool without prompt."""
    grants_path = tmp_path / "data" / "grants.json"
    args = {"cmd": "echo hi > f.txt"}
    store1 = GrantStore(path=grants_path)
    asks_first: list[str] = []
    hook1 = make_approval_hook(
        "on-request",
        confirm=lambda q: asks_first.append(q) or True,
        grant_store=store1,
        session_id="s1",
    )
    assert hook1 is not None
    assert hook1("sandbox_exec", args) is True
    assert len(asks_first) == 1

    # New store instance (simulates restart) — would deny if re-prompted
    store2 = GrantStore(path=grants_path)
    asks: list[str] = []
    hook = make_approval_hook(
        "on-request",
        confirm=lambda q: asks.append(q) or False,
        grant_store=store2,
        session_id="s1",
    )

    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)

    def script(messages: list[dict[str, Any]], tools_payload: list[dict[str, Any]] | None) -> dict[str, Any]:
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": json.dumps(args),
                        },
                    }
                ],
            }
        return {"content": "done"}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
        approval_hook=hook,
    )
    result = asyncio.run(app.run(prompt="make a file", session_id="s1"))
    assert result.status == "completed"
    assert result.tool_calls[0].status == "completed"
    assert (workspace / "f.txt").exists()
    assert asks == []
    # Grant still reusable / recorded
    rows = store2.list()
    assert any(r.get("status") in {"approved", "executed"} for r in rows)


def test_readonly_still_denies_without_grant() -> None:
    hook = make_approval_hook("readonly")
    assert hook is not None
    for name in WRITE_TOOLS:
        assert hook(name, {}) is False
