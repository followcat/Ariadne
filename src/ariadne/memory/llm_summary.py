"""Optional LLM-backed L1 compressor (P3). Grounded extract remains the default."""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from typing import Any, Protocol

from .summary import grounded_compress

_SYSTEM = (
    "You compress a single agent turn into a short factual summary. "
    "Rules: use ONLY facts present in the source text; never invent; "
    "prefer concrete names, paths, numbers, decisions; max 400 characters; "
    "plain text only; no markdown headings."
)

# Bound LLM latency so a hung model cannot wedge the turn forever.
_LLM_TIMEOUT_S = 60.0


class Completer(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> Any: ...


def _run_coro_sync(coro: Any, *, timeout: float = _LLM_TIMEOUT_S) -> Any:
    """Run an async coroutine from sync code even if a loop is already running.

    - No running loop → ``asyncio.run``
    - Running loop → dedicated thread with its own event loop (avoids nested
      ``asyncio.run`` which always fails and used to silent-fallback to grounded)
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    def _thread_main() -> Any:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_thread_main)
        return fut.result(timeout=timeout)


def make_llm_compressor(
    model: Completer,
    *,
    max_chars: int = 400,
    fallback: bool = True,
    timeout: float = _LLM_TIMEOUT_S,
) -> Any:
    """Return a sync compressor(source_text) -> str using an async model.

    Safe to call from ``process_pending`` during an async turn (running loop).
    On failure (or empty model output), falls back to grounded_compress when
    ``fallback`` is True.

    The returned callable has ``.kind == \"llm\"`` for store metadata / tests.
    """

    def compress(source_text: str) -> str:
        src = (source_text or "").strip()
        if not src:
            return ""
        if len(src) <= max_chars // 2:
            return src

        async def _run() -> str:
            exchange = await model.complete(
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": f"Source turn text:\n\n{src[:4000]}\n\nSummary:",
                    },
                ],
                tools=None,
                temperature=0.0,
                max_tokens=min(256, max_chars),
            )
            text = (exchange.message.content or "").strip()
            text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
            text = text.strip("\"'")
            if len(text) > max_chars:
                text = text[:max_chars]
            return text

        try:
            text = _run_coro_sync(_run(), timeout=timeout)
            if text:
                return text
        except Exception:  # noqa: BLE001 — never fail the turn on summarizer
            if not fallback:
                raise
        return grounded_compress(src, max_chars=max_chars)

    compress.kind = "llm"  # type: ignore[attr-defined]
    return compress
