from __future__ import annotations

from dataclasses import dataclass

from .kernel.turn import TurnApplication
from .types import TurnResult


@dataclass
class Agent:
    """Thin façade over TurnApplication — library and CLI share this."""

    turn_app: TurnApplication
    session_id: str = "default"
    model: str | None = None

    async def run(self, prompt: str, *, session_id: str | None = None, model: str | None = None) -> TurnResult:
        return await self.turn_app.run(
            prompt=prompt,
            session_id=session_id or self.session_id,
            model=model or self.model,
        )
