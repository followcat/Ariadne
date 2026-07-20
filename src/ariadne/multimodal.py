"""Image attachments and vision/multimodal capability checks."""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AriadneError, app_error

# Conservative size limits (host-side; providers may be stricter).
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES_PER_TURN = 4
ALLOWED_MIME = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})

# Name heuristics when ARIADNE_VISION=auto (not a guarantee — hosts can force on/off).
_VISION_MODEL_RE = re.compile(
    r"("
    r"vision|gpt-4o|gpt-4\.1|gpt-5|o1|o3|o4|"
    r"claude-3|claude-4|claude-sonnet-4|claude-opus-4|"
    r"gemini|llava|qwen[-_.]?vl|qwen2[-_.]?vl|glm-4v|"
    r"pixtral|phi-3[-_.]?vision|internvl|minicpm[-_.]?v|"
    r"longcat"  # LongCat family may accept images on some gateways
    r")",
    re.I,
)


@dataclass(slots=True)
class ImageAttachment:
    mime: str
    data: bytes
    name: str = "image.png"

    def __post_init__(self) -> None:
        mime = (self.mime or "").lower().strip()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in ALLOWED_MIME:
            raise AriadneError(
                app_error(
                    "ARIADNE_MULTIMODAL_UNSUPPORTED",
                    f"unsupported image type: {self.mime!r} (allowed: png/jpeg/webp/gif)",
                    mime=self.mime,
                )
            )
        if not self.data:
            raise AriadneError(
                app_error("ARIADNE_MULTIMODAL_UNSUPPORTED", "empty image data")
            )
        if len(self.data) > MAX_IMAGE_BYTES:
            raise AriadneError(
                app_error(
                    "ARIADNE_MULTIMODAL_UNSUPPORTED",
                    f"image too large ({len(self.data)} bytes; max {MAX_IMAGE_BYTES})",
                    size=len(self.data),
                )
            )
        self.mime = mime
        if not self.name:
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
            self.name = f"image.{ext.get(mime, 'bin')}"

    def data_url(self) -> str:
        b64 = base64.standard_b64encode(self.data).decode("ascii")
        return f"data:{self.mime};base64,{b64}"

    def openai_image_part(self) -> dict[str, Any]:
        return {"type": "image_url", "image_url": {"url": self.data_url()}}

    def transcript_placeholder(self) -> str:
        return f"[image name={self.name} mime={self.mime} bytes={len(self.data)}]"


def normalize_vision_mode(value: str | None) -> str:
    raw = (value or "auto").strip().lower()
    if raw in {"1", "true", "yes", "on", "force"}:
        return "on"
    if raw in {"0", "false", "no", "off"}:
        return "off"
    return "auto"


def model_supports_vision(model: str, *, vision_mode: str = "auto") -> bool:
    """Whether the host believes *model* can accept image_url content parts."""
    mode = normalize_vision_mode(vision_mode)
    if mode == "on":
        return True
    if mode == "off":
        return False
    return bool(_VISION_MODEL_RE.search(model or ""))


def multimodal_unsupported_error(model: str, *, image_count: int) -> AriadneError:
    return AriadneError(
        app_error(
            "ARIADNE_MULTIMODAL_UNSUPPORTED",
            (
                f"当前模型 {model!r} 未声明多模态/视觉能力，无法发送图片 "
                f"（{image_count} 张）。可：1) 换用支持 vision 的模型；"
                f"2) 设置 ARIADNE_VISION=on 强制尝试（仍可能被服务端拒绝）；"
                f"3) 去掉图片仅发送文字。"
            ),
            model=model,
            image_count=image_count,
            hint="ARIADNE_VISION=auto|on|off",
        )
    )


def ensure_vision_allowed(
    model: str, images: list[ImageAttachment] | None, *, vision_mode: str = "auto"
) -> None:
    if not images:
        return
    if len(images) > MAX_IMAGES_PER_TURN:
        raise AriadneError(
            app_error(
                "ARIADNE_MULTIMODAL_UNSUPPORTED",
                f"too many images ({len(images)}; max {MAX_IMAGES_PER_TURN})",
            )
        )
    if not model_supports_vision(model, vision_mode=vision_mode):
        raise multimodal_unsupported_error(model, image_count=len(images))


def build_user_message_content(
    prompt: str, images: list[ImageAttachment] | None
) -> str | list[dict[str, Any]]:
    """OpenAI-compatible user message content (text or multimodal parts)."""
    text = (prompt or "").strip()
    if not images:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    else:
        parts.append({"type": "text", "text": "(see attached image)"})
    for img in images:
        parts.append(img.openai_image_part())
    return parts


def transcript_user_line(prompt: str, images: list[ImageAttachment] | None) -> str:
    text = (prompt or "").strip()
    if not images:
        return text
    placeholders = " ".join(img.transcript_placeholder() for img in images)
    if text:
        return f"{text}\n{placeholders}"
    return placeholders


def load_image_path(path: Path | str) -> ImageAttachment:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise AriadneError(
            app_error("ARIADNE_MULTIMODAL_UNSUPPORTED", f"image file not found: {p}")
        )
    data = p.read_bytes()
    suffix = p.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/png")
    return ImageAttachment(mime=mime, data=data, name=p.name)


def image_from_base64(mime: str, data_b64: str, *, name: str = "image.png") -> ImageAttachment:
    try:
        raw = base64.standard_b64decode(data_b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise AriadneError(
            app_error("ARIADNE_MULTIMODAL_UNSUPPORTED", f"invalid base64 image: {exc}")
        ) from exc
    return ImageAttachment(mime=mime, data=raw, name=name or "image.png")


def read_clipboard_image() -> ImageAttachment | None:
    """Best-effort clipboard image (Linux xclip / wl-paste; macOS pngpaste optional)."""
    # wl-paste
    if shutil.which("wl-paste"):
        for mime in ("image/png", "image/jpeg", "image/webp"):
            try:
                proc = subprocess.run(
                    ["wl-paste", "--type", mime],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0 and proc.stdout:
                try:
                    return ImageAttachment(mime=mime, data=proc.stdout, name=f"clipboard.{mime.split('/')[-1]}")
                except AriadneError:
                    continue
    # xclip
    if shutil.which("xclip"):
        for mime in ("image/png", "image/jpeg", "image/webp"):
            try:
                proc = subprocess.run(
                    ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if proc.returncode == 0 and proc.stdout and not proc.stdout.startswith(b"Error"):
                # xclip may print error text to stdout on failure
                if proc.stdout.startswith(b"Couldn't") or proc.stdout.startswith(b"Error"):
                    continue
                try:
                    return ImageAttachment(
                        mime=mime, data=proc.stdout, name=f"clipboard.{mime.split('/')[-1]}"
                    )
                except AriadneError:
                    continue
    return None
