from __future__ import annotations

import re
from typing import Any

# obvious secret patterns redacted from traces / tool outputs (SANDBOX §6,
# TOOLCALL §4 rule 4). Patterns are intentionally conservative.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|passwd|token)"
            r"((?:[\"']?\s*[:=]\s*[\"']?))([^\s\"']{6,})"
        ),
        r"\1\2***",
    ),
]

# Structured payloads need a key-aware boundary in addition to value regexes.
# Keep this conservative: false positives lose diagnostic detail, while false
# negatives can persist credentials into traces or long-lived memory.
_SECRET_KEY = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|secret|password|passwd|token|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|"
    r"authorization|cookie|credential)(?:$|[_-])"
)


def _normalize_key(value: Any) -> str:
    """Normalize structured keys before matching secret components."""

    text = str(value)
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").casefold()


def _is_secret_key(value: Any) -> bool:
    normalized = _normalize_key(value)
    return bool(normalized and _SECRET_KEY.search(normalized))


def redact_text(text: str) -> str:
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking strings in trace payloads."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: ("***" if _is_secret_key(k) else redact_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    return value
