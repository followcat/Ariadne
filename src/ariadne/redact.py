from __future__ import annotations

import re
from typing import Any

# obvious secret patterns redacted from traces / tool outputs (SANDBOX §6,
# TOOLCALL §4 rule 4). Patterns are intentionally conservative.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer ***"),
    (
        re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)([\"'\s:=]{1,3})([^\s\"']{6,})"),
        r"\1\2***",
    ),
]


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
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    return value
