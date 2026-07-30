from __future__ import annotations

import re
from typing import Any

# Obvious value patterns plus structural assignment parsing (SANDBOX §6,
# TOOLCALL §4 rule 4). Secret key recognition has one implementation below;
# text assignments do not maintain a second finite credential-name list.
_OPENAI_KEY = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_AUTHORIZATION_SCHEME = re.compile(
    r"(?i)(?P<prefix>\bauthorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<scheme>bearer|basic|token|apikey)\s+"
    r"(?P<credential>[^\s,;]+)"
)
_BEARER_VALUE = re.compile(
    r"(?i)\b(?P<scheme>bearer)\s+(?P<credential>[^\s,;]+)"
)
_ASSIGNMENT = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_.-]{1,95})"
    r"(?P<separator>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\s\"',;}\]]+)"
)
_AUTHORIZATION_SCHEMES = {"bearer", "basic", "token", "apikey"}

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
    """Redact credential assignments, including camelCase key names."""

    out = _OPENAI_KEY.sub("sk-***", text)
    out = _AUTHORIZATION_SCHEME.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('scheme')} ***"
        ),
        out,
    )
    out = _BEARER_VALUE.sub(
        lambda match: f"{match.group('scheme')} ***",
        out,
    )

    def replace_assignment(match: re.Match[str]) -> str:
        key = match.group("key")
        value = match.group("value")
        if not _is_secret_key(key):
            return match.group(0)
        if (
            _normalize_key(key) == "authorization"
            and value.casefold() in _AUTHORIZATION_SCHEMES
        ):
            # The scheme-specific pass already replaced its following value.
            return match.group(0)
        return f"{key}{match.group('separator')}***"

    return _ASSIGNMENT.sub(replace_assignment, out)


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
