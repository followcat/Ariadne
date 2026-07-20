"""Official plugins: gitlab / redmine / odoo against a fake HTTP server."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.plugins import PLUGIN_REGISTRY, PluginStore, build_plugin_tools
from ariadne.tools.registry import ToolContext

REQUESTS: list[dict] = []


class FakeHandler(BaseHTTPRequestHandler):
    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        REQUESTS.append(
            {
                "method": method,
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )
        if self.path.startswith("/api/v4/projects/") and "merge_requests" in self.path:
            payload = [{"iid": 1, "title": "Add feature"}]
        elif self.path.startswith("/api/v4/"):
            payload = {"ok": True}
        elif self.path.startswith("/issues.json"):
            payload = {"issues": [{"id": 7, "subject": "Bug"}]}
        elif self.path == "/jsonrpc":
            rpc = json.loads(body) if body else {}
            service = rpc.get("params", {}).get("service")
            if service == "common":
                payload = {"jsonrpc": "2.0", "id": 1, "result": 42}
            else:
                payload = {"jsonrpc": "2.0", "id": 1, "result": [{"id": 1, "name": "Partner A"}]}
        else:
            payload = {"raw": True}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = lambda self: FakeHandler._handle(self, "GET")
    do_POST = lambda self: FakeHandler._handle(self, "POST")

    def log_message(self, *args) -> None:
        pass


@pytest.fixture()
def fake_server():
    REQUESTS.clear()
    server = HTTPServer(("127.0.0.1", 0), FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _ctx() -> ToolContext:
    return ToolContext(session_id="s", turn_id="t", sandbox=None)


def test_gitlab_plugin_tools(fake_server: str) -> None:
    specs = build_plugin_tools("gitlab", {"url": fake_server, "token": "glpat-x"})
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"gitlab_request", "gitlab_list_merge_requests"}

    async def run() -> None:
        result = await by_name["gitlab_list_merge_requests"].handler(
            {"project": "group/proj", "state": "opened"}, _ctx()
        )
        assert result == [{"iid": 1, "title": "Add feature"}]
        generic = await by_name["gitlab_request"].handler({"path": "user"}, _ctx())
        assert generic == {"ok": True}

    asyncio.run(run())
    mr_req = REQUESTS[0]
    assert "group%2Fproj" in mr_req["path"]
    assert mr_req["headers"].get("Private-Token") == "glpat-x"


def test_redmine_plugin_tools(fake_server: str) -> None:
    specs = build_plugin_tools("redmine", {"url": fake_server, "api_key": "rm-key"})
    by_name = {s.name: s for s in specs}

    async def run() -> None:
        result = await by_name["redmine_list_issues"].handler({"project_id": 3}, _ctx())
        assert result["issues"][0]["id"] == 7

    asyncio.run(run())
    req = REQUESTS[0]
    assert req["headers"].get("X-Redmine-Api-Key") == "rm-key"
    assert "project_id=3" in req["path"]


def test_odoo_plugin_search_read(fake_server: str) -> None:
    specs = build_plugin_tools(
        "odoo",
        {"url": fake_server, "database": "db1", "login": "admin", "password": "pw"},
    )
    by_name = {s.name: s for s in specs}

    async def run() -> None:
        result = await by_name["odoo_search_read"].handler(
            {"model": "res.partner", "limit": 5}, _ctx()
        )
        assert result == [{"id": 1, "name": "Partner A"}]

    asyncio.run(run())
    # first call authenticates, second executes
    auth = json.loads(REQUESTS[0]["body"])
    assert auth["params"]["service"] == "common"
    call = json.loads(REQUESTS[1]["body"])
    args = call["params"]["args"]
    assert args[0] == "db1" and args[1] == 42 and args[3] == "res.partner"
    assert args[4] == "search_read"


def test_missing_config_fastfails() -> None:
    with pytest.raises(AriadneError) as excinfo:
        build_plugin_tools("gitlab", {"url": "http://x"})
    assert excinfo.value.error.code == "ARIADNE_PLUGIN_ERROR"
    with pytest.raises(AriadneError):
        build_plugin_tools("nonexistent", {})


def test_plugin_store_enable_disable(tmp_path: Path) -> None:
    path = tmp_path / "plugins.json"
    store = PluginStore(path)
    assert not path.exists(), "list/read must not create the file"
    assert store.list() == {}
    store.enable("gitlab", {"url": "http://x", "token": "t"})
    assert store.enabled() == {"gitlab": {"url": "http://x", "token": "t"}}
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600
    store.disable("gitlab")
    assert store.enabled() == {}
    with pytest.raises(AriadneError):
        store.disable("gitlab")


def test_compose_merges_user_then_workspace_plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI: ~/.ariadne/plugins.json is a user attribute; workspace overrides."""
    from ariadne.config import Settings
    from ariadne.host.compose import compose_agent

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    PluginStore(home / ".ariadne" / "plugins.json").enable(
        "gitlab", {"url": "http://user-gitlab", "token": "user-tok"}
    )
    PluginStore(data / "plugins.json").enable(
        "gitlab", {"url": "http://ws-gitlab", "token": "ws-tok"}
    )
    PluginStore(home / ".ariadne" / "plugins.json").enable(
        "redmine", {"url": "http://user-redmine", "api_key": "rk"}
    )

    settings = Settings(
        workspace=workspace,
        data_dir=data,
        base_url="http://example.invalid",
        api_key="k",
        model="m",
        sandbox="null",
        merge_home_plugins=True,
    )
    agent = compose_agent(settings)

    async def names(a) -> set[str]:
        return {t["name"] for t in await a.list_tools()}

    tools = asyncio.run(names(agent))
    assert "gitlab_request" in tools
    assert "redmine_list_issues" in tools

    # workspace config wins for gitlab on name clash:
    merged: dict[str, dict[str, str]] = {}
    merged.update(PluginStore(home / ".ariadne" / "plugins.json").enabled())
    merged.update(PluginStore(data / "plugins.json").enabled())
    assert merged["gitlab"]["url"] == "http://ws-gitlab"
    assert merged["redmine"]["url"] == "http://user-redmine"

    # web path: no home merge
    settings_web = Settings(
        workspace=workspace,
        data_dir=data,
        base_url="http://example.invalid",
        api_key="k",
        model="m",
        sandbox="null",
        merge_home_plugins=False,
    )
    agent_web = compose_agent(settings_web)
    tools_web = asyncio.run(names(agent_web))
    assert "gitlab_request" in tools_web
    assert "redmine_list_issues" not in tools_web
