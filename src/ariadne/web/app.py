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

    @app.post("/api/turns")
    async def run_turn(body: TurnBody, username: str = Depends(current_user)) -> Any:
        user_settings = _settings_for(username)
        if body.session_id:
            user_settings = dataclasses.replace(user_settings, session_id=body.session_id)
        agent = compose_agent(user_settings)
        result = await agent.run(body.input)
        return json.loads(render_json(result))

    @app.get("/api/turns/stream")
    async def stream_turn(
        input: str, session_id: str | None = None, username: str = Depends(current_user)
    ) -> StreamingResponse:
        user_settings = _settings_for(username)
        if session_id:
            user_settings = dataclasses.replace(user_settings, session_id=session_id)
        agent = compose_agent(user_settings)

        async def events():
            async for event in agent.run_stream(input):
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

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/sessions")
    def sessions(username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import list_sessions

        user_data = settings.resolved_data_dir / "web" / "users" / username
        return [
            {"session_id": s.session_id, "turns": s.turns, "mtime": s.mtime}
            for s in list_sessions(user_data)
        ]

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
