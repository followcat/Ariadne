"""Web host HTTP-level tests (httpx ASGI transport)."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import httpx
import pytest

from ariadne.config import load_settings
from ariadne.web.app import create_app


def _client(tmp_path: Path) -> httpx.AsyncClient:
    settings = dataclasses.replace(
        load_settings(workspace=tmp_path / "ws"), data_dir=tmp_path / "data"
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def test_register_login_me(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "alice", "password": "password123"}
            )
            assert r.status_code == 200, r.text
            token = r.json()["token"]
            # duplicate register rejected
            r = await client.post(
                "/api/auth/register", json={"username": "alice", "password": "password123"}
            )
            assert r.status_code == 400
            # short password rejected
            r = await client.post(
                "/api/auth/register", json={"username": "bob", "password": "short"}
            )
            assert r.status_code == 400
            # login
            r = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "password123"}
            )
            assert r.status_code == 200 and r.json()["token"] == token
            # wrong password
            r = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrongpassword"}
            )
            assert r.status_code == 401
            # /api/me with and without token
            r = await client.get("/api/me")
            assert r.status_code == 401
            r = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            assert r.json()["username"] == "alice"
            assert r.json()["provider_configured"] is False

    asyncio.run(run())


def test_provider_binding(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "carol", "password": "password123"}
            )
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            # incomplete provider rejected
            r = await client.put(
                "/api/me/provider", json={"base_url": "", "api_key": "k", "model": "m"}, headers=headers
            )
            assert r.status_code == 400
            r = await client.put(
                "/api/me/provider",
                json={"base_url": "https://api.example.com/v1/", "api_key": "k", "model": "m"},
                headers=headers,
            )
            assert r.status_code == 200
            r = await client.get("/api/me", headers=headers)
            body = r.json()
            assert body["provider_configured"] is True
            assert body["base_url"] == "https://api.example.com/v1"  # trailing slash stripped
            assert "api_key" not in body, "api key must not be echoed"
            # turn without provider errors for unconfigured user
            r2 = await client.post(
                "/api/auth/register", json={"username": "dave", "password": "password123"}
            )
            r = await client.post(
                "/api/turns",
                json={"input": "hi"},
                headers={"Authorization": f"Bearer {r2.json()['token']}"},
            )
            assert r.status_code == 400

    asyncio.run(run())


def test_index_served(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.get("/")
            assert r.status_code == 200
            assert "Ariadne" in r.text

    asyncio.run(run())


def test_users_file_permissions(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            await client.post(
                "/api/auth/register", json={"username": "erin", "password": "password123"}
            )

    asyncio.run(run())
    users_file = tmp_path / "data" / "web" / "users.json"
    assert users_file.exists()
    assert (users_file.stat().st_mode & 0o777) == 0o600


def test_sessions_api(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "sessuser", "password": "password123"}
            )
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            # create empty session id
            r = await client.post("/api/sessions", headers=headers)
            assert r.status_code == 200
            sid = r.json()["session_id"]
            assert sid.startswith("web-")
            # brand-new session has empty history
            r = await client.get(f"/api/sessions/{sid}", headers=headers)
            assert r.status_code == 200
            assert r.json()["messages"] == []
            assert r.json()["exists"] is False
            # write a transcript as the runtime would
            from ariadne.cli.sessions import session_path

            user_data = tmp_path / "data" / "web" / "users" / "sessuser"
            path = session_path(user_data, sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                '{"role":"user","content":"hello session"}\n'
                '{"role":"assistant","content":"hi there"}\n',
                encoding="utf-8",
            )
            r = await client.get("/api/sessions", headers=headers)
            assert r.status_code == 200
            rows = r.json()
            assert any(s["session_id"] == sid for s in rows)
            row = next(s for s in rows if s["session_id"] == sid)
            assert row["turns"] == 1
            assert "hello" in row["preview"]
            r = await client.get(f"/api/sessions/{sid}", headers=headers)
            body = r.json()
            msgs = body["messages"]
            assert msgs == [
                {"role": "user", "content": "hello session"},
                {"role": "assistant", "content": "hi there"},
            ]
            # auto title summary
            r = await client.patch(
                f"/api/sessions/{sid}",
                json={"refresh_title": True},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["title"]
            assert r.json()["title_source"] == "auto"
            # user title override
            r = await client.patch(
                f"/api/sessions/{sid}",
                json={"title": "我的会话主题"},
                headers=headers,
            )
            assert r.status_code == 200 and r.json()["title"] == "我的会话主题"
            r = await client.get("/api/sessions", headers=headers)
            row = next(s for s in r.json() if s["session_id"] == sid)
            assert row["title"] == "我的会话主题"
            assert row["title_source"] == "user"
            # isolation: other user sees no sessions
            r2 = await client.post(
                "/api/auth/register", json={"username": "other", "password": "password123"}
            )
            r = await client.get(
                "/api/sessions", headers={"Authorization": f"Bearer {r2.json()['token']}"}
            )
            assert r.json() == []
            # delete
            r = await client.delete(f"/api/sessions/{sid}", headers=headers)
            assert r.status_code == 200
            r = await client.delete(f"/api/sessions/{sid}", headers=headers)
            assert r.status_code == 404

    asyncio.run(run())


def test_workspace_browse_api(tmp_path: Path) -> None:
    """List / read / file serve under sandbox workspace (Codex-style browser)."""

    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "wsuser", "password": "password123"}
            )
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            ws = tmp_path / "ws"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "notes.md").write_text("# hello\nline2\n", encoding="utf-8")
            (ws / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            sub = ws / "subdir"
            sub.mkdir()
            (sub / "data.json").write_text('{"a": 1}\n', encoding="utf-8")
            # auth required
            r = await client.get("/api/workspace/list")
            assert r.status_code == 401
            # list root
            r = await client.get("/api/workspace/list", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["path"] == "/workspace"
            assert body["parent"] is None
            names = {e["name"]: e for e in body["entries"]}
            assert "notes.md" in names and names["notes.md"]["kind"] == "file"
            assert "subdir" in names and names["subdir"]["kind"] == "dir"
            assert "plot.png" in names
            # list subdir via virtual path
            r = await client.get(
                "/api/workspace/list",
                params={"path": "/workspace/subdir"},
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["path"] == "/workspace/subdir"
            assert body["parent"] == "/workspace"
            assert any(e["name"] == "data.json" for e in body["entries"])
            # path escape rejected
            r = await client.get(
                "/api/workspace/list",
                params={"path": "/etc"},
                headers=headers,
            )
            assert r.status_code == 400
            r = await client.get(
                "/api/workspace/read",
                params={"path": "/workspace/../ws/notes.md"},
                headers=headers,
            )
            # ".." in rel parts or resolve outside root
            assert r.status_code in {400, 404}
            # read text
            r = await client.get(
                "/api/workspace/read",
                params={"path": "/workspace/notes.md"},
                headers=headers,
            )
            assert r.status_code == 200
            body = r.json()
            assert body["binary"] is False
            assert "hello" in body["text"]
            assert body["name"] == "notes.md"
            # binary flagged
            r = await client.get(
                "/api/workspace/read",
                params={"path": "/workspace/plot.png"},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["binary"] is True
            # file blob with auth
            r = await client.get(
                "/api/workspace/file",
                params={"path": "/workspace/plot.png"},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.content[:4] == b"\x89PNG"
            assert "image/png" in r.headers.get("content-type", "")
            # host absolute path under workspace (models often print real FS paths)
            host_png = str((ws / "plot.png").resolve())
            r = await client.get(
                "/api/workspace/file",
                params={"path": host_png},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.content[:4] == b"\x89PNG"
            # host absolute outside workspace rejected
            r = await client.get(
                "/api/workspace/file",
                params={"path": "/etc/passwd"},
                headers=headers,
            )
            assert r.status_code in {400, 404}
            # /api/me exposes workspace root for UI path rewrite
            r = await client.get("/api/me", headers=headers)
            assert r.status_code == 200
            assert r.json().get("workspace") == str(ws.resolve())

    asyncio.run(run())


def test_per_user_plugins(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "frank", "password": "password123"}
            )
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}
            # list: all three official plugins, none enabled
            r = await client.get("/api/me/plugins", headers=headers)
            assert r.status_code == 200
            plugins = {p["name"]: p for p in r.json()}
            assert set(plugins) == {"gitlab", "odoo", "redmine"}
            assert not any(p["enabled"] for p in plugins.values())
            # missing config -> 400
            r = await client.put(
                "/api/me/plugins/gitlab", json={"config": {"url": "http://x"}}, headers=headers
            )
            assert r.status_code == 400
            # enable with full config
            r = await client.put(
                "/api/me/plugins/gitlab",
                json={
                    "config": {
                        "url": "http://gitlab.example.com",
                        "token": "glpat-secret-token-xyz",
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200
            r = await client.get("/api/me/plugins", headers=headers)
            gitlab = {p["name"]: p for p in r.json()}["gitlab"]
            assert gitlab["enabled"] is True
            # secrets are masked for display (never raw token)
            assert gitlab["config"]["url"] == "http://gitlab.example.com"
            assert "*****" in gitlab["config"]["token"]
            assert "glpat-secret-token-xyz" not in gitlab["config"]["token"]
            # re-enable with masked token keeps the stored secret
            r = await client.put(
                "/api/me/plugins/gitlab",
                json={
                    "config": {
                        "url": "http://gitlab.example.com",
                        "token": gitlab["config"]["token"],
                    }
                },
                headers=headers,
            )
            assert r.status_code == 200
            # other user does not see it (user attribute isolation)
            r2 = await client.post(
                "/api/auth/register", json={"username": "grace", "password": "password123"}
            )
            r = await client.get(
                "/api/me/plugins", headers={"Authorization": f"Bearer {r2.json()['token']}"}
            )
            assert {p["name"]: p["enabled"] for p in r.json()}["gitlab"] is False
            # disable
            r = await client.delete("/api/me/plugins/gitlab", headers=headers)
            assert r.status_code == 200
            r = await client.delete("/api/me/plugins/gitlab", headers=headers)
            assert r.status_code == 400, "double disable fastfails"
            # unknown plugin 404
            r = await client.put(
                "/api/me/plugins/nope", json={"config": {}}, headers=headers
            )
            assert r.status_code == 404

    asyncio.run(run())
