"""FastAPI web host: auth, BYOK provider binding, turns + SSE, sessions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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

    def _settings_for(username: str) -> Settings:
        provider = users.get_provider(username)
        if not provider:
            raise HTTPException(status_code=400, detail="provider not configured (PUT /api/me/provider)")
        user_data = settings.resolved_data_dir / "web" / "users" / username
        return dataclasses.replace(
            settings,
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
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
        return {
            "username": username,
            "provider_configured": bool(provider),
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
        }

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
        if session_id:
            user_settings = dataclasses.replace(user_settings, session_id=session_id)
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
            }
            for s in list_sessions(user_data)
        ]

    @app.post("/api/sessions")
    def create_session(username: str = Depends(current_user)) -> Any:
        """Allocate a new empty session id (transcript created on first turn)."""
        import secrets

        sid = f"web-{secrets.token_hex(4)}"
        return {"session_id": sid}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import load_session_messages, session_exists

        user_data = _user_data(username)
        if not session_exists(user_data, session_id):
            # Brand-new id (not yet written) — empty history is fine for UI
            return {"session_id": session_id, "messages": [], "exists": False}
        messages = load_session_messages(user_data, session_id, limit=200)
        return {"session_id": session_id, "messages": messages, "exists": True}

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

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
