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
                json={"config": {"url": "http://gitlab.example.com", "token": "t1"}},
                headers=headers,
            )
            assert r.status_code == 200
            r = await client.get("/api/me/plugins", headers=headers)
            assert {p["name"]: p["enabled"] for p in r.json()}["gitlab"] is True
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
