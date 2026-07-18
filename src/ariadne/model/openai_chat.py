from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, AsyncIterator

from ..errors import AriadneError, app_error
from ..types import Message, Usage
from .base import ModelExchange, ModelStreamEvent


class OpenAIChatModel:
    """OpenAI-compatible Chat Completions client with optional SSE streaming."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 120.0) -> None:
        if not base_url:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "BASE_URL is required"))
        if not api_key:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "API_KEY is required"))
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ariadne/0.2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
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
            raise AriadneError(app_error("ARIADNE_MODEL_ERROR", f"{type(exc).__name__}: {exc}")) from exc

    def _parse_message(self, obj: dict[str, Any]) -> ModelExchange:
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
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        obj = self._request(payload)
        return self._parse_message(obj)

    async def stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
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
                "Accept": "text/event-stream",
                "User-Agent": "ariadne/0.2.0",
            },
            method="POST",
        )
        content_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        usage = Usage()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text or text.startswith(":"):
                        continue
                    if not text.startswith("data:"):
                        continue
                    data_s = text[5:].strip()
                    if data_s == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_s)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        u = chunk["usage"]
                        usage = Usage(
                            prompt_tokens=int(u.get("prompt_tokens") or 0),
                            completion_tokens=int(u.get("completion_tokens") or 0),
                            total_tokens=int(u.get("total_tokens") or 0),
                        )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            piece = str(delta["content"])
                            content_parts.append(piece)
                            yield ModelStreamEvent(kind="delta", text=piece)
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index") or 0)
                            acc = tool_acc.setdefault(
                                idx,
                                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                            )
                            if tc.get("id"):
                                acc["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                acc["function"]["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["function"]["arguments"] += fn["arguments"]
        except urllib.error.HTTPError as exc:
            # fallback to non-stream if provider rejects stream
            err_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in {400, 404, 415, 501}:
                exchange = await self.complete(
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    model=model,
                )
                if exchange.message.content:
                    yield ModelStreamEvent(kind="delta", text=exchange.message.content)
                yield ModelStreamEvent(kind="completed", exchange=exchange)
                return
            raise AriadneError(
                app_error("ARIADNE_MODEL_ERROR", f"Model HTTP {exc.code}", body=err_body[:500])
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(app_error("ARIADNE_MODEL_ERROR", f"{type(exc).__name__}: {exc}")) from exc

        tool_calls = [tool_acc[i] for i in sorted(tool_acc)] or None
        message = Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls,
        )
        exchange = ModelExchange(message=message, usage=usage, raw={"streamed": True})
        yield ModelStreamEvent(kind="completed", exchange=exchange)
