"""Multimodal / vision helpers and turn gating."""

from __future__ import annotations

import asyncio
import base64

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.multimodal import (
    ImageAttachment,
    build_user_message_content,
    ensure_vision_allowed,
    model_supports_vision,
    transcript_user_line,
)
from ariadne.sandbox.null import NullSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def test_model_supports_vision_heuristics() -> None:
    assert model_supports_vision("gpt-4o", vision_mode="auto") is True
    assert model_supports_vision("LongCat-2.0", vision_mode="auto") is True
    assert model_supports_vision("kimi-k2.7-code", vision_mode="auto") is False
    assert model_supports_vision("kimi-k2.7-code", vision_mode="on") is True
    assert model_supports_vision("gpt-4o", vision_mode="off") is False


def test_build_user_message_content_with_image() -> None:
    png = base64.standard_b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    img = ImageAttachment(mime="image/png", data=png, name="dot.png")
    content = build_user_message_content("describe", [img])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "[image" in transcript_user_line("hi", [img])


def test_ensure_vision_allowed_raises() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    img = ImageAttachment(mime="image/png", data=png, name="x.png")
    with pytest.raises(AriadneError) as exc:
        ensure_vision_allowed("text-only-model", [img], vision_mode="auto")
    assert exc.value.error.code == "ARIADNE_MULTIMODAL_UNSUPPORTED"


def test_turn_refuses_images_when_vision_off(tmp_path) -> None:
    png = base64.standard_b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    img = ImageAttachment(mime="image/png", data=png, name="dot.png")

    def script(messages, tools):
        return {"role": "assistant", "content": "ok", "tool_calls": None}

    model = FakeModel(script=script, model="plain-text-model")
    memory = Memory.local(path=tmp_path / "m")
    skills = SkillStore.from_dirs([], strict=False)
    tools = build_default_registry(memory=memory, skills=skills)
    app = TurnApplication(
        model=model,
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=NullSandbox(),
        vision_mode="off",
    )

    async def run() -> None:
        with pytest.raises(AriadneError) as exc:
            await app.run(
                prompt="see image",
                session_id="s1",
                images=[img],
            )
        assert exc.value.error.code == "ARIADNE_MULTIMODAL_UNSUPPORTED"

    asyncio.run(run())


def test_turn_sends_multimodal_content_when_vision_on(tmp_path) -> None:
    png = base64.standard_b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    img = ImageAttachment(mime="image/png", data=png, name="dot.png")
    seen: list = []

    def script(messages, tools):
        seen.append(messages)
        return {"role": "assistant", "content": "I see a pixel", "tool_calls": None}

    model = FakeModel(script=script, model="vision-model")
    memory = Memory.local(path=tmp_path / "m2")
    skills = SkillStore.from_dirs([], strict=False)
    tools = build_default_registry(memory=memory, skills=skills)
    app = TurnApplication(
        model=model,
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=NullSandbox(),
        vision_mode="on",
    )

    async def run() -> None:
        result = await app.run(prompt="what is this?", session_id="s2", images=[img])
        assert result.status == "completed"
        assert "pixel" in result.text

    asyncio.run(run())
    user_msgs = [m for m in seen[0] if m.get("role") == "user"]
    assert user_msgs
    assert isinstance(user_msgs[-1]["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msgs[-1]["content"])
