"""In-process memory worker: drain L1 summaries + projection jobs.

Personal v1 does not require a separate OS process. Hosts may:

1. Call :meth:`MemoryWorker.run_once` after turns (or on a timer)
2. Run ``ariadne memory-worker --once`` / loop from CLI
3. Rely on inline ``process_pending`` during summary render (default path)

Projection default projector returns no ops (``no_change``) so lag clears when
state was already applied via the ``conversation_state`` tool. Hosts may inject
an LLM projector later without changing the queue protocol.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

from .facade import MemoryFacade
from .projection import ProjectorFn

# Default projector: evidence was already reduced by tools; mark no_change.
async def _default_projector(evidence: str, turn_id: str) -> list[dict[str, Any]]:
    _ = evidence
    _ = turn_id
    return []


def make_projector(
    fn: Callable[[str, str], Awaitable[list[dict[str, Any]]] | list[dict[str, Any]]],
) -> ProjectorFn:
    """Wrap a sync or async callable as a :data:`ProjectorFn` (LLM plug-in hook).

    Hosts can pass an LLM client that returns closed-schema ops with evidence
    quotes; the worker never invents a default LLM dependency.
    """

    async def _wrapped(evidence: str, turn_id: str) -> list[dict[str, Any]]:
        result = fn(evidence, turn_id)
        if asyncio.iscoroutine(result):
            result = await result
        if not isinstance(result, list):
            raise TypeError("projector must return a list of ops")
        return result

    return _wrapped


@dataclass
class MemoryWorker:
    """Drain pending summary + projection work for a MemoryFacade."""

    memory: MemoryFacade
    worker_id: str = "local"
    projector: ProjectorFn = field(default=_default_projector)
    summary_batch: int = 32
    projection_batch: int = 20

    def run_summaries(self, *, session_id: str | None = None) -> int:
        return self.memory.summaries.process_pending(
            session_id=session_id, max_jobs=self.summary_batch
        )

    async def run_projection(self) -> list[dict[str, Any]]:
        if self.memory.projection is None:
            return []
        return await self.memory.projection.drain(
            self.projector, max_jobs=self.projection_batch
        )

    async def run_once(self, *, session_id: str | None = None) -> dict[str, Any]:
        """Process one batch of summaries + projection jobs."""
        n_sum = self.run_summaries(session_id=session_id)
        proj = await self.run_projection()
        return {
            "summaries_processed": n_sum,
            "projection_results": proj,
            "projection_count": len(proj),
            "pending_summaries": self.memory.summaries.pending_count(session_id),
            "pending_projection": (
                sum(
                    self.memory.projection.pending_lag(sid)
                    for sid in {
                        str(j.get("session_id") or "")
                        for j in (self.memory.projection.list_jobs() if self.memory.projection else [])
                    }
                    if sid
                )
                if self.memory.projection
                else 0
            ),
        }

    async def run_loop(
        self,
        *,
        interval_seconds: float = 2.0,
        max_iterations: int | None = None,
        stop_when_idle: bool = False,
        on_tick: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> int:
        """Repeated drain. Returns number of iterations run."""
        n = 0
        while max_iterations is None or n < max_iterations:
            result = await self.run_once()
            n += 1
            if on_tick is not None:
                maybe = on_tick(result)
                if asyncio.iscoroutine(maybe):
                    await maybe
            idle = (
                result["summaries_processed"] == 0
                and result["projection_count"] == 0
            )
            if stop_when_idle and idle:
                break
            if max_iterations is not None and n >= max_iterations:
                break
            await asyncio.sleep(max(0.05, interval_seconds))
        return n
