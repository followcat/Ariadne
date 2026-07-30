from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from .kernel.turn import TurnApplication
from .sandbox.active import ActiveSessionManager
from .types import RunTurnCommand, TurnEvent, TurnResult


@dataclass
class Agent:
    turn_app: TurnApplication
    session_id: str = "default"
    model: str | None = None
    active_sessions: ActiveSessionManager | None = None

    async def run(
        self,
        input: str | RunTurnCommand,
        *,
        session_id: str | None = None,
        model: str | None = None,
        user_id: str | None = None,
        tool_loop_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
        on_event: Any = None,
        images: list[Any] | None = None,
    ) -> TurnResult:
        if isinstance(input, RunTurnCommand):
            return await self.turn_app.run_command(input)
        return await self.turn_app.run(
            prompt=input,
            session_id=session_id or self.session_id,
            model=model or self.model,
            user_id=user_id,
            tool_loop_limit=tool_loop_limit,
            metadata=metadata,
            on_event=on_event,
            images=images,
        )

    async def run_stream(
        self,
        input: str | RunTurnCommand,
        *,
        session_id: str | None = None,
        model: str | None = None,
        images: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        if isinstance(input, RunTurnCommand):
            command = input
            prompt = command.input if isinstance(command.input, str) else "\n".join(
                m.content for m in command.input if m.role == "user"
            )
            sid = command.session_id
            mdl = command.model
            imgs = images
            meta = command.metadata
        else:
            prompt = input
            sid = session_id or self.session_id
            mdl = model or self.model
            imgs = images
            meta = metadata
        async for event in self.turn_app.run_events(
            prompt=prompt,
            session_id=sid,
            model=mdl,
            images=imgs,
            metadata=meta,
        ):
            yield event

    # Inspection helpers (convenience reads; no second execution path)

    async def list_skills(self) -> list[dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "keywords": s.keywords,
                "requires_tools": s.requires_tools,
                "namespace": s.namespace,
                "version": s.version,
            }
            for s in self.turn_app.skills.list()
        ]

    async def list_tools(self, *, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "catalog_description": spec.catalog_description,
                "exposure": spec.tool_exposure,
                "side_effect_level": spec.side_effect_level,
                "network_access": spec.network_access,
                "idempotent": spec.idempotent,
                "required_credentials": list(spec.required_credentials),
            }
            for spec in self.turn_app.tools.tools.values()
            if spec.tool_exposure != "hidden"
        ]

    async def get_curated(self, *, session_id: str | None = None) -> dict[str, Any]:
        return self.turn_app.memory.get_curated(session_id=session_id or self.session_id)
