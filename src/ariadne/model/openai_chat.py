from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..errors import AriadneError, app_error
from ..types import Message, Usage


@dataclass(slots=True)
class ModelExchange:
    message: Message
    usage: Usage
    raw: dict[str, Any]


class OpenAIChatModel:
    """Minimal OpenAI-compatible Chat Completions client."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        if not base_url:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "BASE_URL is required"))
        if not api_key:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "API_KEY is required"))
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> ModelExchange:
        # Keep async surface; implementation is sync HTTP for MVP simplicity.
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ariadne/0.0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                obj = json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise AriadneError(
                app_error(
                    "ARIADNE_MODEL_ERROR",
                    f"Model HTTP {exc.code}",
                    status=exc.code,
                    body=err_body[:500],
                )
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(
                app_error("ARIADNE_MODEL_ERROR", f"{type(exc).__name__}: {exc}")
            ) from exc

        choices = obj.get("choices") or []
        if not choices:
            raise AriadneError(app_error("ARIADNE_MODEL_ERROR", "Model returned no choices"))
        msg = choices[0].get("message") or {}
        usage_raw = obj.get("usage") or {}
        details = usage_raw.get("completion_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
        )
        message = Message(
            role="assistant",
            content=str(msg.get("content") or ""),
            tool_calls=msg.get("tool_calls"),
        )
        return ModelExchange(message=message, usage=usage, raw=obj)
