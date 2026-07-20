"""Optional LLM-backed L1 compressor (P3). Grounded extract remains the default."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

from .summary import grounded_compress

_SYSTEM = (
    "You compress a single agent turn into a short factual summary. "
    "Rules: use ONLY facts present in the source text; never invent; "
    "prefer concrete names, paths, numbers, decisions; max 400 characters; "
    "plain text only; no markdown headings."
)


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


def make_llm_compressor(
    model: Completer,
    *,
    max_chars: int = 400,
    fallback: bool = True,
) -> Any:
    """Return a sync compressor(source_text) -> str using an async model.

    Runs the model in a fresh event loop when called from sync process_pending.
    On failure (or empty model output), falls back to grounded_compress when
    ``fallback`` is True.
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
            # Strip wrapping quotes/code fences if the model adds them.
            text = re.sub(r"^```[a-z]*\n?|\n?```$", "", text).strip()
            text = text.strip("\"'")
            if len(text) > max_chars:
                text = text[:max_chars]
            return text

        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                text = asyncio.run(_run())
            else:
                # Already in a loop: cannot asyncio.run; use grounded fallback path
                # unless caller schedules process_pending via worker async side.
                if fallback:
                    return grounded_compress(src, max_chars=max_chars)
                raise RuntimeError("llm compressor cannot nest event loops")
            if text:
                return text
        except Exception:  # noqa: BLE001 — never fail the turn on summarizer
            if not fallback:
                raise
        return grounded_compress(src, max_chars=max_chars)

    return compress
