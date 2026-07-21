"""FastAPI web host: auth, BYOK provider binding, turns + SSE, sessions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..cli.render import render_json
from ..config import Settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from .users import UserStore

STATIC_DIR = Path(__file__).parent / "static"


class RegisterBody(BaseModel):
    username: str
    password: str


class ProviderBody(BaseModel):
    base_url: str
    api_key: str
    model: str


class TurnBody(BaseModel):
    input: str
    session_id: str | None = None


class ImagePart(BaseModel):
    mime: str
    data_base64: str
    name: str | None = None


class TurnStreamBody(BaseModel):
    input: str = ""
    session_id: str | None = None
    images: list[ImagePart] = []


class SessionPatchBody(BaseModel):
    title: str | None = None
    refresh_title: bool = False
    force: bool = False  # force auto-refresh even if user-set title


class PluginBody(BaseModel):
    config: dict[str, str]


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Ariadne Web")
    users = UserStore(settings.resolved_data_dir / "web" / "users.json")

    def current_user(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        username = users.username_for_token(token) if token else None
        if username is None:
            raise HTTPException(status_code=401, detail="invalid or missing token")
        return username

    def _project_root() -> Path:
        """Serve-process project folder (CLI cwd / --workspace). Always the project root."""
        root = settings.workspace.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _user_data_dir(username: str) -> Path:
        return settings.resolved_data_dir / "web" / "users" / username

    def _workspace_root_for(username: str) -> Path:
        """Active /workspace binding for this account (design/web-workspace.md).

        project  — shared serve workspace (Codex-like open folder)
        per_user — durable tree under the account data dir
        """
        mode = (settings.web_workspace_mode or "project").strip().lower()
        if mode == "per_user":
            root = _user_data_dir(username) / "workspace"
            root.mkdir(parents=True, exist_ok=True)
            return root.resolve()
        if mode != "project":
            raise HTTPException(
                status_code=500,
                detail=f"invalid web_workspace_mode: {mode!r}",
            )
        return _project_root()

    def _settings_for(username: str) -> Settings:
        provider = users.get_provider(username)
        if not provider:
            raise HTTPException(status_code=400, detail="provider not configured (PUT /api/me/provider)")
        user_data = _user_data_dir(username)
        user_data.mkdir(parents=True, exist_ok=True)
        # Agent sandbox /workspace must match browse APIs for this user + mode.
        return dataclasses.replace(
            settings,
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            workspace=_workspace_root_for(username),
            data_dir=user_data,
            merge_home_plugins=False,  # web users only get their own plugins
        )

    @app.post("/api/auth/register")
    def register(body: RegisterBody) -> dict[str, str]:
        try:
            token = users.register(body.username, body.password)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"token": token, "username": body.username}

    @app.post("/api/auth/login")
    def login(body: RegisterBody) -> dict[str, str]:
        try:
            token = users.login(body.username, body.password)
        except AriadneError as exc:
            raise HTTPException(status_code=401, detail=exc.error.message) from exc
        return {"token": token, "username": body.username}

    @app.get("/api/me")
    def me(username: str = Depends(current_user)) -> dict[str, Any]:
        provider = users.get_provider(username)
        ws = _workspace_root_for(username)
        return {
            "username": username,
            "provider_configured": bool(provider),
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
            # Active /workspace host path — models often print real FS paths; UI maps them.
            "workspace": str(ws),
            "workspace_mode": settings.web_workspace_mode,
            "project_root": str(_project_root()),
        }

    def _resolve_workspace_path(
        raw_path: str, *, username: str, must_exist: bool = True
    ) -> Path:
        """Map /workspace/... , relative, or host-absolute-under-root into active workspace."""
        root = _workspace_root_for(username)
        raw = (raw_path or "").strip() or "/workspace"
        if raw in {"/workspace", "workspace", ".", ""}:
            target = root
            rel = ""
        elif raw.startswith("/workspace/"):
            rel = raw[len("/workspace/") :]
            target = (root / rel).resolve()
        elif raw.startswith("workspace/"):
            rel = raw[len("workspace/") :]
            target = (root / rel).resolve()
        elif raw.startswith("/"):
            # Host absolute path (e.g. /home/…/project/plot.png). Models often print
            # real FS paths; allow only when resolved path stays under workspace root.
            try:
                candidate = Path(raw).expanduser().resolve()
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
            if candidate != root and root not in candidate.parents:
                raise HTTPException(
                    status_code=400,
                    detail="path must be under /workspace (sandbox root)",
                )
            target = candidate
            rel = "" if candidate == root else candidate.relative_to(root).as_posix()
        else:
            rel = raw
            target = (root / rel).resolve()
        if rel and ".." in Path(rel).parts:
            raise HTTPException(status_code=400, detail="path escapes workspace")
        if root not in target.parents and target != root:
            raise HTTPException(status_code=400, detail="path escapes workspace")
        if must_exist and not target.exists():
            raise HTTPException(status_code=404, detail=f"path not found: {raw}")
        return target

    def _virtual_path(target: Path, *, username: str) -> str:
        root = _workspace_root_for(username)
        if target == root:
            return "/workspace"
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path escapes workspace") from exc
        return f"/workspace/{rel}"

    @app.get("/api/workspace/list")
    def workspace_list(
        path: str = Query(default="/workspace", description="Directory under /workspace"),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        """List a directory in the sandbox workspace (Codex-style file browser)."""
        target = _resolve_workspace_path(path, username=username)
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="path is not a directory")
        # Hide common noise / secrets at listing time
        skip_names = {
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".env",
            ".DS_Store",
        }
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                target.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot list: {exc}") from exc
        for child in children:
            name = child.name
            if name in skip_names or name.startswith(".env"):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            kind = "dir" if child.is_dir() else "file"
            entries.append(
                {
                    "name": name,
                    "path": _virtual_path(child, username=username),
                    "kind": kind,
                    "size": int(st.st_size) if kind == "file" else 0,
                    "mtime": float(st.st_mtime),
                }
            )
        parent = target.parent
        root = _workspace_root_for(username)
        parent_path = None
        if target != root and (root in parent.parents or parent == root):
            parent_path = _virtual_path(parent, username=username)
        return {
            "path": _virtual_path(target, username=username),
            "parent": parent_path,
            "entries": entries,
            "workspace": str(root),
            "workspace_mode": settings.web_workspace_mode,
            "project_root": str(_project_root()),
        }

    @app.get("/api/workspace/read")
    def workspace_read(
        path: str = Query(..., description="File path under /workspace"),
        max_bytes: int = Query(default=512_000, ge=1024, le=2_000_000),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Read a text file from workspace (preview). Binary files are flagged."""
        target = _resolve_workspace_path(path, username=username)
        if not target.is_file():
            raise HTTPException(status_code=400, detail="path is not a file")
        size = target.stat().st_size
        raw = target.read_bytes()[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        # Heuristic: treat as binary if NUL or high ratio of non-text
        binary = b"\x00" in raw[:4096]
        text = ""
        encoding = "utf-8"
        if not binary:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("gb18030")
                    encoding = "gb18030"
                except UnicodeDecodeError:
                    binary = True
                    text = ""
        vpath = _virtual_path(target, username=username)
        return {
            "path": vpath,
            "name": target.name,
            "size": size,
            "truncated": truncated,
            "binary": binary,
            "encoding": encoding if not binary else None,
            "text": text if not binary else None,
            "download_path": f"/api/workspace/file?path={vpath}",
        }

    @app.get("/api/workspace/file")
    def workspace_file(
        path: str = Query(..., description="Sandbox path e.g. /workspace/plot.png"),
        username: str = Depends(current_user),
    ) -> FileResponse:
        """Serve a file from the account's active /workspace root.

        Used by the web UI to inline 走势图 / plots written by sandbox tools.
        Auth required; path confined to project or per_user root (web-workspace.md).
        """
        target = _resolve_workspace_path(path, username=username)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {path}")
        media = "application/octet-stream"
        suffix = target.suffix.lower()
        if suffix in {".png"}:
            media = "image/png"
        elif suffix in {".jpg", ".jpeg"}:
            media = "image/jpeg"
        elif suffix in {".gif"}:
            media = "image/gif"
        elif suffix in {".webp"}:
            media = "image/webp"
        elif suffix in {".svg"}:
            media = "image/svg+xml"
        elif suffix in {".txt", ".md", ".py", ".json", ".csv", ".log", ".yml", ".yaml"}:
            media = "text/plain; charset=utf-8"
        return FileResponse(target, media_type=media, filename=target.name)

    @app.put("/api/me/provider")
    def put_provider(body: ProviderBody, username: str = Depends(current_user)) -> dict[str, str]:
        try:
            users.set_provider(
                username, base_url=body.base_url, api_key=body.api_key, model=body.model
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "ok"}

    def _parse_images(parts: list[ImagePart] | None) -> list[Any]:
        from ..multimodal import image_from_base64

        if not parts:
            return []
        out = []
        for part in parts:
            out.append(
                image_from_base64(
                    part.mime,
                    part.data_base64,
                    name=part.name or "image.png",
                )
            )
        return out

    @app.post("/api/turns")
    async def run_turn(body: TurnBody, username: str = Depends(current_user)) -> Any:
        user_settings = _settings_for(username)
        if body.session_id:
            user_settings = dataclasses.replace(user_settings, session_id=body.session_id)
        agent = compose_agent(user_settings)
        try:
            result = await agent.run(body.input)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return json.loads(render_json(result))

    async def _sse_for_turn(
        *,
        username: str,
        text: str,
        session_id: str | None,
        images: list[Any] | None,
    ) -> StreamingResponse:
        user_settings = _settings_for(username)
        # SSE path always wants model token streaming (answer + thinking deltas).
        if session_id:
            user_settings = dataclasses.replace(
                user_settings, session_id=session_id, stream=True
            )
        else:
            user_settings = dataclasses.replace(user_settings, stream=True)
        agent = compose_agent(user_settings)

        async def events():
            try:
                async for event in agent.run_stream(text, images=images or None):
                    if event.kind in {"turn_completed", "turn_failed"}:
                        result = event.data.get("result")
                        payload = {
                            "kind": event.kind,
                            "result": json.loads(render_json(result)) if result else None,
                        }
                    else:
                        payload = {"kind": event.kind, "data": event.data}
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    await asyncio.sleep(0)
            except AriadneError as exc:
                # Surface multimodal / config failures as a terminal SSE event
                payload = {
                    "kind": "turn_failed",
                    "error": {
                        "code": exc.error.code,
                        "message": exc.error.message,
                        "details": exc.error.details,
                    },
                    "result": None,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/turns/stream")
    async def stream_turn(
        input: str, session_id: str | None = None, username: str = Depends(current_user)
    ) -> StreamingResponse:
        # Text-only GET (kept for simple clients / e2e)
        return await _sse_for_turn(
            username=username, text=input, session_id=session_id, images=None
        )

    @app.post("/api/turns/stream")
    async def stream_turn_post(
        body: TurnStreamBody, username: str = Depends(current_user)
    ) -> StreamingResponse:
        try:
            images = _parse_images(body.images)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return await _sse_for_turn(
            username=username,
            text=body.input or "",
            session_id=body.session_id,
            images=images,
        )

    def _user_data(username: str) -> Path:
        return settings.resolved_data_dir / "web" / "users" / username

    @app.get("/api/sessions")
    def sessions(username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import list_sessions

        user_data = _user_data(username)
        return [
            {
                "session_id": s.session_id,
                "turns": s.turns,
                "mtime": s.mtime,
                "preview": s.preview,
                "title": s.title,
                "title_source": s.title_source,
            }
            for s in list_sessions(user_data)
        ]

    @app.post("/api/sessions")
    def create_session(username: str = Depends(current_user)) -> Any:
        """Allocate a new empty session id (transcript created on first turn)."""
        import secrets

        sid = f"web-{secrets.token_hex(4)}"
        return {"session_id": sid, "title": "", "title_source": ""}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import get_session_title, load_session_messages, session_exists

        user_data = _user_data(username)
        title, source = get_session_title(user_data, session_id)
        if not session_exists(user_data, session_id):
            # Brand-new id (not yet written) — empty history is fine for UI
            return {
                "session_id": session_id,
                "messages": [],
                "exists": False,
                "title": title,
                "title_source": source,
            }
        messages = load_session_messages(user_data, session_id, limit=200)
        return {
            "session_id": session_id,
            "messages": messages,
            "exists": True,
            "title": title,
            "title_source": source,
        }

    @app.patch("/api/sessions/{session_id}")
    def patch_session(
        session_id: str, body: SessionPatchBody, username: str = Depends(current_user)
    ) -> Any:
        """Set or auto-refresh the session topic title."""
        from ..cli.sessions import (
            get_session_title,
            refresh_session_title,
            session_exists,
            set_session_title,
        )

        user_data = _user_data(username)
        if body.refresh_title:
            if not session_exists(user_data, session_id):
                raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
            meta = refresh_session_title(user_data, session_id, force=body.force)
            if meta is None:
                raise HTTPException(status_code=400, detail="cannot title an empty session")
            return {
                "session_id": session_id,
                "title": meta.get("title", ""),
                "title_source": meta.get("source", "auto"),
                "skipped": bool(meta.get("skipped")),
            }
        if body.title is not None:
            try:
                meta = set_session_title(user_data, session_id, body.title, source="user")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "session_id": session_id,
                "title": meta["title"],
                "title_source": meta["source"],
            }
        title, source = get_session_title(user_data, session_id)
        return {"session_id": session_id, "title": title, "title_source": source}

    @app.delete("/api/sessions/{session_id}")
    def remove_session(session_id: str, username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import delete_session

        ok = delete_session(_user_data(username), session_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        return {"status": "deleted", "session_id": session_id}

    def _plugin_store_for(username: str) -> Any:
        from ..plugins import PluginStore

        user_data = settings.resolved_data_dir / "web" / "users" / username
        return PluginStore(user_data / "plugins.json")

    @app.get("/api/me/plugins")
    def list_my_plugins(username: str = Depends(current_user)) -> Any:
        from ..plugins import PLUGIN_REGISTRY
        from ..plugins.store import display_config

        configured = _plugin_store_for(username).list()
        return [
            {
                "name": name,
                "description": plugin.description,
                "required_config": list(plugin.required_config),
                "enabled": bool(configured.get(name, {}).get("enabled")),
                "configured": name in configured,
                # secrets masked (middle *****); never return raw tokens
                "config": display_config(
                    dict((configured.get(name) or {}).get("config") or {})
                ),
            }
            for name, plugin in sorted(PLUGIN_REGISTRY.items())
        ]

    @app.put("/api/me/plugins/{name}")
    def enable_plugin(name: str, body: PluginBody, username: str = Depends(current_user)) -> Any:
        from ..plugins import PLUGIN_REGISTRY, build_plugin_tools
        from ..plugins.store import looks_masked_value

        plugin = PLUGIN_REGISTRY.get(name)
        if plugin is None:
            raise HTTPException(status_code=404, detail=f"unknown plugin: {name}")
        store = _plugin_store_for(username)
        existing = dict((store.list().get(name) or {}).get("config") or {})
        # Keep stored secrets when the client resubmits a masked placeholder
        # or leaves a secret field blank on re-enable.
        merged: dict[str, str] = {}
        missing: list[str] = []
        for key in plugin.required_config:
            raw = str(body.config.get(key) or "").strip()
            if not raw or looks_masked_value(raw):
                if existing.get(key):
                    merged[key] = str(existing[key])
                else:
                    missing.append(key)
            else:
                merged[key] = raw
        if missing:
            raise HTTPException(status_code=400, detail=f"missing config: {', '.join(missing)}")
        try:
            build_plugin_tools(name, merged)  # validate before persisting
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        store.enable(name, merged)
        return {"status": "enabled", "plugin": name}

    @app.delete("/api/me/plugins/{name}")
    def disable_plugin(name: str, username: str = Depends(current_user)) -> Any:
        try:
            _plugin_store_for(username).disable(name)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "disabled", "plugin": name}

    # Vue SPA build (frontend/ → static/dist). Fall back to legacy static/index.html.
    dist_dir = STATIC_DIR / "dist"
    dist_index = dist_dir / "index.html"
    legacy_index = STATIC_DIR / "index.html"

    if dist_dir.is_dir():
        assets = dist_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="vue-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        if dist_index.is_file():
            return FileResponse(dist_index)
        return FileResponse(legacy_index)

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa_fallback(spa_path: str) -> FileResponse:
        """Serve built SPA files or index for client routes; never shadow /api."""
        if spa_path.startswith("api"):
            raise HTTPException(status_code=404, detail="not found")
        if dist_dir.is_dir():
            candidate = dist_dir / spa_path
            if candidate.is_file():
                return FileResponse(candidate)
            if dist_index.is_file():
                return FileResponse(dist_index)
        raise HTTPException(status_code=404, detail="not found")

    return app
