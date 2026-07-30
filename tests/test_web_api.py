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
        load_settings(workspace=tmp_path / "ws"),
        data_dir=tmp_path / "data",
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


def test_user_model_and_skill_patch_host_edit_surfaces(tmp_path: Path) -> None:
    async def run() -> None:
        async with _client(tmp_path) as client:
            registered = await client.post(
                "/api/auth/register",
                json={"username": "modeler", "password": "password123"},
            )
            token = registered.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}

            created = await client.post(
                "/api/me/user-model",
                headers=headers,
                json={
                    "type": "preference",
                    "key": "answer_style",
                    "value": "concise",
                    "confidence": 1.0,
                    "scope": "user",
                },
            )
            assert created.status_code == 200, created.text
            entry = created.json()
            listed = await client.get("/api/me/user-model", headers=headers)
            assert listed.json()["entries"][0]["value"] == "concise"

            changed = await client.put(
                f"/api/me/user-model/{entry['entry_id']}",
                headers=headers,
                json={
                    "type": "preference",
                    "key": "answer_style",
                    "value": "detailed",
                    "confidence": 1.0,
                    "scope": "user",
                    "expected_revision": 1,
                },
            )
            assert changed.status_code == 200
            stale = await client.put(
                f"/api/me/user-model/{entry['entry_id']}",
                headers=headers,
                json={
                    "type": "preference",
                    "key": "answer_style",
                    "value": "brief",
                    "confidence": 1.0,
                    "scope": "user",
                    "expected_revision": 1,
                },
            )
            assert stale.status_code == 409

            skill_dir = (
                tmp_path
                / "data"
                / "web"
                / "users"
                / "modeler"
                / "skills"
                / "user"
                / "planner"
            )
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: planner\ndescription: plan\nversion: \"1\"\n---\n\nold\n",
                encoding="utf-8",
            )
            proposed = await client.post(
                "/api/me/skills/planner/patch-proposals",
                headers=headers,
                json={
                    "description": "plan verified steps",
                    "body": "new",
                    "keywords": ["verify"],
                    "evidence": ["user corrected turn t1"],
                    "expected_version": "1",
                },
            )
            assert proposed.status_code == 200, proposed.text
            proposal_id = proposed.json()["proposal_id"]
            assert "old" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
            confirmed = await client.post(
                f"/api/me/skill-patches/{proposal_id}/confirm",
                headers=headers,
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["version"] == "2"
            assert "new" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")

            correction = await client.post(
                "/api/me/skill-outcomes/corrections",
                headers=headers,
                json={
                    "turn_id": "t1",
                    "skill_name": "planner",
                    "reason": "the recommendation was wrong",
                },
            )
            assert correction.status_code == 200
            disabled = await client.put(
                "/api/me/skill-outcomes/ranking",
                headers=headers,
                json={"enabled": False},
            )
            assert disabled.json()["ranking_enabled"] is False

            workspace = tmp_path / "ws"
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "ready.flag").write_text("ready", encoding="utf-8")
            scheduled = await client.post(
                "/api/me/scheduled-goals",
                headers=headers,
                json={
                    "session_id": "s1",
                    "goal": "notify when ready",
                    "check": {"kind": "path_exists", "spec": {"path": "ready.flag"}},
                    "interval_seconds": 60,
                    "next_run_at": 0,
                },
            )
            assert scheduled.status_code == 200, scheduled.text
            ran = await client.post(
                "/api/me/scheduled-goals/run-due",
                headers=headers,
            )
            assert ran.status_code == 200
            assert ran.json()["results"][0]["status"] == "completed"
            notifications = await client.get(
                "/api/me/goal-notifications",
                headers=headers,
            )
            assert notifications.json()["notifications"][0]["kind"] == "goal_satisfied"

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
            assert len(msgs) == 2
            assert msgs[0]["role"] == "user" and msgs[0]["content"] == "hello session"
            assert msgs[0].get("turn_index") == 1
            assert msgs[1]["role"] == "assistant" and msgs[1]["content"] == "hi there"
            assert msgs[1].get("turn_index") == 1
            assert "turns" in body
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
            body = r.json()
            assert body.get("workspace") == str(ws.resolve())
            assert body.get("workspace_binding") == "project"
            assert body.get("project_root") == str(ws.resolve())
            assert "workspace_mode" not in body

    asyncio.run(run())


def test_web_open_folder_shared_across_accounts_and_sessions(tmp_path: Path) -> None:
    """Ordinary chats bind the serve open folder (shared; not per-account trees)."""

    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register", json={"username": "alice", "password": "password123"}
            )
            alice = r.json()["token"]
            ha = {"Authorization": f"Bearer {alice}"}
            r = await client.post(
                "/api/auth/register", json={"username": "bob", "password": "password123"}
            )
            bob = r.json()["token"]
            hb = {"Authorization": f"Bearer {bob}"}

            r = await client.get("/api/me", headers=ha)
            assert r.status_code == 200
            ma = r.json()
            assert ma["workspace_binding"] == "project"
            open_ws = Path(ma["workspace"]).resolve()
            assert open_ws == (tmp_path / "ws").resolve()
            assert Path(ma["project_root"]).resolve() == open_ws

            r = await client.get("/api/me", headers=hb)
            assert Path(r.json()["workspace"]).resolve() == open_ws

            open_ws.mkdir(parents=True, exist_ok=True)
            (open_ws / "shared.txt").write_text("same-tree", encoding="utf-8")

            r = await client.get("/api/workspace/list", headers=ha)
            assert r.status_code == 200
            assert r.json()["workspace_binding"] == "project"
            names_a = {e["name"] for e in r.json()["entries"]}
            assert "shared.txt" in names_a

            r = await client.get("/api/workspace/list", headers=hb)
            names_b = {e["name"] for e in r.json()["entries"]}
            assert "shared.txt" in names_b

            r = await client.get(
                "/api/workspace/file",
                params={"path": "/workspace/shared.txt"},
                headers=hb,
            )
            assert r.status_code == 200
            assert r.content == b"same-tree"

    asyncio.run(run())


def test_web_atelier_branch_workspace_isolated_from_main(tmp_path: Path) -> None:
    """Atelier branch list/file root differs from main and from the open folder."""

    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register",
                json={"username": "wsalice", "password": "password123"},
            )
            headers = {"Authorization": f"Bearer {r.json()['token']}"}
            r = await client.post(
                "/api/ateliers",
                json={"name": "bind-lab"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            aid = r.json()["id"]

            r = await client.get(
                "/api/workspace/list",
                params={"atelier_id": aid, "atelier_session": "main"},
                headers=headers,
            )
            assert r.status_code == 200
            main_body = r.json()
            assert main_body["workspace_binding"] == "atelier"
            main_root = Path(main_body["workspace"]).resolve()
            open_folder = (tmp_path / "ws").resolve()
            assert main_root != open_folder
            (main_root / "main-only.txt").write_text("main", encoding="utf-8")

            r = await client.post(
                f"/api/ateliers/{aid}/branches",
                json={"name": "exp-iso"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            branch_id = r.json()["id"]

            r = await client.get(
                "/api/workspace/list",
                params={"atelier_id": aid, "atelier_session": branch_id},
                headers=headers,
            )
            assert r.status_code == 200
            br_body = r.json()
            assert br_body["workspace_binding"] == "atelier"
            branch_root = Path(br_body["workspace"]).resolve()
            assert branch_root != main_root
            assert branch_root != open_folder
            (branch_root / "branch-only.txt").write_text("branch", encoding="utf-8")

            r = await client.get(
                "/api/workspace/list",
                params={"atelier_id": aid, "atelier_session": "main"},
                headers=headers,
            )
            main_names = {e["name"] for e in r.json()["entries"]}
            assert "main-only.txt" in main_names
            assert "branch-only.txt" not in main_names

            r = await client.get(
                "/api/workspace/list",
                params={"atelier_id": aid, "atelier_session": branch_id},
                headers=headers,
            )
            br_names = {e["name"] for e in r.json()["entries"]}
            assert "branch-only.txt" in br_names

            # Branch cannot serve main-only host path when scoped to branch
            r = await client.get(
                "/api/workspace/file",
                params={
                    "path": str(main_root / "main-only.txt"),
                    "atelier_id": aid,
                    "atelier_session": branch_id,
                },
                headers=headers,
            )
            assert r.status_code in {400, 404}

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


def test_atelier_api_crud_and_knowledge(tmp_path: Path) -> None:
    """Web Atelier: create, branch, knowledge add/modify/remove, workspace bind."""

    async def run() -> None:
        async with _client(tmp_path) as client:
            r = await client.post(
                "/api/auth/register",
                json={"username": "atelieruser", "password": "password123"},
            )
            token = r.json()["token"]
            headers = {"Authorization": f"Bearer {token}"}

            r = await client.get("/api/ateliers", headers=headers)
            assert r.status_code == 200 and r.json() == []

            r = await client.post(
                "/api/ateliers",
                json={"name": "demo-app", "no_scan": True},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["id"] == "demo-app"
            aid = body["id"]

            # Chinese display name → auto id, keep 中文 name
            r = await client.post(
                "/api/ateliers",
                json={"name": "画画", "no_scan": True},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            zh = r.json()
            assert zh["name"] == "画画"
            assert zh["id"].startswith("atelier-")
            r = await client.delete(
                f"/api/ateliers/{zh['id']}?yes=true", headers=headers
            )
            assert r.status_code == 200

            r = await client.get(f"/api/ateliers/{aid}", headers=headers)
            assert r.status_code == 200
            assert any(s["id"] == "main" for s in r.json()["sessions"])

            # knowledge get + apply add/modify/remove
            r = await client.get(f"/api/ateliers/{aid}/knowledge", headers=headers)
            assert r.status_code == 200
            body_k = r.json()["content"]
            assert "我想记住的" in body_k or "决策" in body_k or "小本本" in body_k

            # Power API still supports structured apply; primary UX is full PUT edit.
            r = await client.post(
                f"/api/ateliers/{aid}/knowledge/apply",
                json={
                    "updates": [
                        {
                            "section": "我想记住的",
                            "type": "add",
                            "new_text": "采用 SQLite 起步",
                        }
                    ]
                },
                headers=headers,
            )
            assert r.status_code == 200 and r.json()["changed"] is True
            assert "SQLite" in r.json()["content"]

            r = await client.post(
                f"/api/ateliers/{aid}/knowledge/apply",
                json={
                    "updates": [
                        {
                            "section": "我想记住的",
                            "type": "modify",
                            "old_text": "SQLite",
                            "new_text": "采用 Postgres 生产库",
                        }
                    ]
                },
                headers=headers,
            )
            assert r.status_code == 200
            assert "Postgres" in r.json()["content"]
            assert "SQLite 起步" not in r.json()["content"]

            r = await client.post(
                f"/api/ateliers/{aid}/knowledge/apply",
                json={
                    "updates": [
                        {
                            "section": "我想记住的",
                            "type": "remove",
                            "old_text": "Postgres",
                        }
                    ]
                },
                headers=headers,
            )
            assert r.status_code == 200
            assert "Postgres" not in r.json()["content"]

            # full put
            r = await client.put(
                f"/api/ateliers/{aid}/knowledge",
                json={"content": "# demo\n\n## 约定\n- hello\n"},
                headers=headers,
            )
            assert r.status_code == 200
            r = await client.get(f"/api/ateliers/{aid}/knowledge", headers=headers)
            assert "hello" in r.json()["content"]
            assert len(r.json()["history"]) >= 1

            # branch lifecycle
            r = await client.post(
                f"/api/ateliers/{aid}/branches",
                json={"name": "exp-auth", "initial_message": "try JWT"},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["id"] == "branch-exp-auth"

            r = await client.get(
                f"/api/ateliers/{aid}/sessions/branch-exp-auth/messages",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["type"] == "branch"

            r = await client.post(
                f"/api/ateliers/{aid}/branches/exp-auth/merge",
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["status"] == "merged"

            r = await client.post(
                f"/api/ateliers/{aid}/branches",
                json={"name": "throwaway"},
                headers=headers,
            )
            assert r.status_code == 200
            r = await client.post(
                f"/api/ateliers/{aid}/branches/throwaway/discard",
                headers=headers,
            )
            assert r.status_code == 200 and r.json()["status"] == "discarded"

            # workspace browser scoped to atelier
            r = await client.get(
                "/api/workspace/list",
                params={"atelier_id": aid},
                headers=headers,
            )
            assert r.status_code == 200
            assert r.json()["workspace_binding"] == "atelier"
            assert r.json()["atelier_id"] == aid

            # isolation: other user cannot see
            r2 = await client.post(
                "/api/auth/register",
                json={"username": "otheratelier", "password": "password123"},
            )
            other = {"Authorization": f"Bearer {r2.json()['token']}"}
            r = await client.get("/api/ateliers", headers=other)
            assert r.json() == []
            r = await client.get(f"/api/ateliers/{aid}", headers=other)
            assert r.status_code == 404

            r = await client.delete(f"/api/ateliers/{aid}?yes=true", headers=headers)
            assert r.status_code == 200

    asyncio.run(run())
