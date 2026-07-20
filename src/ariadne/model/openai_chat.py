from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, AsyncIterator

from ..errors import AriadneError, app_error
from ..types import Message, Usage
from .base import ModelExchange, ModelStreamEvent

_THINK_OPEN = re.compile(r"<think(?:ing)?>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think(?:ing)?>", re.IGNORECASE)


def _split_think_tags(content: str) -> tuple[str, str]:
    """Split finished content into (visible, reasoning) via <think> tags."""
    if not content:
        return "", ""
    reasoning_parts: list[str] = []
    visible_parts: list[str] = []
    pos = 0
    while pos < len(content):
        m_open = _THINK_OPEN.search(content, pos)
        if not m_open:
            visible_parts.append(content[pos:])
            break
        visible_parts.append(content[pos : m_open.start()])
        m_close = _THINK_CLOSE.search(content, m_open.end())
        if not m_close:
            # Unclosed: treat remainder as reasoning
            reasoning_parts.append(content[m_open.end() :])
            break
        reasoning_parts.append(content[m_open.end() : m_close.start()])
        pos = m_close.end()
    return "".join(visible_parts).strip(), "".join(reasoning_parts).strip()


class _ThinkStreamSplitter:
    """Incremental splitter for streamed content that may contain <think> tags."""

    def __init__(self) -> None:
        self.in_think = False
        self.carry = ""  # incomplete tag prefix

    def feed(self, piece: str) -> list[tuple[str, str]]:
        """Return list of (channel, text) where channel is 'think' | 'content'."""
        out: list[tuple[str, str]] = []
        s = self.carry + (piece or "")
        self.carry = ""
        i = 0
        while i < len(s):
            if self.in_think:
                m_close = _THINK_CLOSE.search(s, i)
                if m_close:
                    frag = s[i : m_close.start()]
                    if frag:
                        out.append(("think", frag))
                    self.in_think = False
                    i = m_close.end()
                    continue
                # Maybe partial close tag at end
                partial = _partial_suffix_match(s[i:], ("</think>", "</thinking>", "</think", "</thinking", "</", "<"))
                if partial:
                    body = s[i : len(s) - partial]
                    if body:
                        out.append(("think", body))
                    self.carry = s[len(s) - partial :]
                    break
                out.append(("think", s[i:]))
                break
            m_open = _THINK_OPEN.search(s, i)
            if m_open:
                frag = s[i : m_open.start()]
                if frag:
                    out.append(("content", frag))
                self.in_think = True
                i = m_open.end()
                continue
            # Maybe partial open tag at end
            partial = _partial_suffix_match(
                s[i:], ("<think>", "<thinking>", "<think", "<thinking", "<")
            )
            if partial:
                body = s[i : len(s) - partial]
                if body:
                    out.append(("content", body))
                self.carry = s[len(s) - partial :]
                break
            out.append(("content", s[i:]))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.carry:
            return []
        # Incomplete tag: treat as content/think body of current channel
        ch = "think" if self.in_think else "content"
        frag = self.carry
        self.carry = ""
        return [(ch, frag)] if frag else []


def _partial_suffix_match(text: str, candidates: tuple[str, ...]) -> int:
    """If text ends with a prefix of any candidate, return that prefix length."""
    best = 0
    for cand in candidates:
        for n in range(1, min(len(cand), len(text)) + 1):
            if text.endswith(cand[:n]) and n > best:
                # Prefer longest match that is a true prefix of cand
                if cand.startswith(text[-n:]):
                    best = n
    return best


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

    @staticmethod
    def _extract_reasoning(msg_or_delta: dict[str, Any]) -> str:
        """Pull provider-specific reasoning / thinking fields.

        Common OpenAI-compatible shapes:
        - reasoning_content (DeepSeek / many forks)
        - reasoning (string or {content|text})
        - thinking / thinking_content
        """
        if not isinstance(msg_or_delta, dict):
            return ""
        for key in ("reasoning_content", "thinking_content", "thinking"):
            val = msg_or_delta.get(key)
            if isinstance(val, str) and val:
                return val
        reasoning = msg_or_delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            return reasoning
        if isinstance(reasoning, dict):
            for key in ("content", "text", "summary"):
                val = reasoning.get(key)
                if isinstance(val, str) and val:
                    return val
        return ""

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
        content = str(msg.get("content") or "")
        reasoning = self._extract_reasoning(msg)
        # Some models wrap CoT as <think>...</think> inside content.
        if content and ("<think" in content.lower()):
            vis, tag_r = _split_think_tags(content)
            content = vis
            if tag_r:
                reasoning = (reasoning + "\n" + tag_r).strip() if reasoning else tag_r
        message = Message(
            role="assistant",
            content=content,
            tool_calls=msg.get("tool_calls"),
            reasoning_content=reasoning,
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
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, Any]] = {}
        usage = Usage()
        splitter = _ThinkStreamSplitter()
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
                        details = u.get("completion_tokens_details") or {}
                        usage = Usage(
                            prompt_tokens=int(u.get("prompt_tokens") or 0),
                            completion_tokens=int(u.get("completion_tokens") or 0),
                            total_tokens=int(u.get("total_tokens") or 0),
                            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
                        )
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        rpiece = self._extract_reasoning(delta)
                        if rpiece:
                            reasoning_parts.append(rpiece)
                            yield ModelStreamEvent(kind="thinking_delta", text=rpiece)
                        if delta.get("content"):
                            piece = str(delta["content"])
                            for channel, frag in splitter.feed(piece):
                                if not frag:
                                    continue
                                if channel == "think":
                                    reasoning_parts.append(frag)
                                    yield ModelStreamEvent(kind="thinking_delta", text=frag)
                                else:
                                    content_parts.append(frag)
                                    yield ModelStreamEvent(kind="delta", text=frag)
                        for tc in delta.get("tool_calls") or []:
                            idx = int(tc.get("index") or 0)
                            acc = tool_acc.setdefault(
                                idx,
                                {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                },
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
                if exchange.message.reasoning_content:
                    yield ModelStreamEvent(
                        kind="thinking_delta", text=exchange.message.reasoning_content
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

        for channel, frag in splitter.flush():
            if not frag:
                continue
            if channel == "think":
                reasoning_parts.append(frag)
                yield ModelStreamEvent(kind="thinking_delta", text=frag)
            else:
                content_parts.append(frag)
                yield ModelStreamEvent(kind="delta", text=frag)

        tool_calls = [tool_acc[i] for i in sorted(tool_acc)] or None
        message = Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_parts),
        )
        exchange = ModelExchange(message=message, usage=usage, raw={"streamed": True})
        yield ModelStreamEvent(kind="completed", exchange=exchange)
