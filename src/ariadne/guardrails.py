"""Input/output boundary guardrails.

In-bound:  user input is scanned for pasted secrets (redacted before it
           reaches the model AND the transcript) and for common prompt-
           injection phrases (warning only, never silently blocked).
Out-bound: assistant final text passes secret redaction before it is
           shown, traced, or persisted.

Findings are always surfaced — guardrails warn/redact explicitly, they
never silently alter behavior (DESIGN_PRINCIPLES fastfail spirit).
"""

from __future__ import annotations

from dataclasses import dataclass

from .redact import redact_text

# common injection openers (lowercase match); warning-only by design
INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard your instructions",
    "disregard previous instructions",
    "forget your instructions",
    "you are now ",
    "new system prompt",
    "reveal your system prompt",
    "print your system prompt",
)


@dataclass(slots=True)
class GuardFinding:
    kind: str  # "secret" | "injection"
    detail: str


def scan_input(text: str) -> tuple[str, list[GuardFinding]]:
    """Redact pasted secrets, flag injection phrasing. Returns (safe_text, findings)."""
    findings: list[GuardFinding] = []
    safe = redact_text(text)
    if safe != text:
        findings.append(
            GuardFinding(kind="secret", detail="pasted secret redacted from model input and transcript")
        )
    lowered = text.lower()
    for marker in INJECTION_MARKERS:
        if marker in lowered:
            findings.append(
                GuardFinding(kind="injection", detail=f"possible prompt-injection phrase: {marker!r}")
            )
            break
    return safe, findings


def scan_output(text: str) -> tuple[str, list[GuardFinding]]:
    """Redact secrets from assistant output. Returns (safe_text, findings)."""
    safe = redact_text(text)
    if safe != text:
        return safe, [GuardFinding(kind="secret", detail="secret redacted from assistant output")]
    return text, []
