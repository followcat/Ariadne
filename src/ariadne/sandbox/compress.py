from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class CompressionResult:
    stdout: str
    stderr: str
    truncated: bool = False
    compressed: bool = False


_LOG_LINE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}|INFO|DEBUG|WARN|ERROR|TRACE)\b", re.I)


def compress_observation(
    *,
    stdout: str,
    stderr: str,
    max_stdout_bytes: int = 256_000,
    max_stderr_bytes: int = 64_000,
    strategy: str = "auto",
) -> CompressionResult:
    """Observation compression beyond naive single cut.

    Strategies:
    - head_tail: keep head+tail with marker
    - log_dedupe: collapse repeated log lines then head_tail
    - auto: choose log_dedupe when log-like, else head_tail
    """
    out = stdout or ""
    err = stderr or ""
    compressed = False
    if strategy == "auto":
        strategy = "log_dedupe" if _looks_like_logs(out) else "head_tail"
    if strategy == "log_dedupe":
        new_out, c1 = _dedupe_lines(out)
        new_err, c2 = _dedupe_lines(err)
        compressed = c1 or c2
        out, err = new_out, new_err
    out2, t1 = _head_tail(out, max_stdout_bytes)
    err2, t2 = _head_tail(err, max_stderr_bytes)
    if t1 or t2:
        compressed = True
    return CompressionResult(stdout=out2, stderr=err2, truncated=t1 or t2, compressed=compressed)


def _looks_like_logs(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    hits = sum(1 for ln in lines[:50] if _LOG_LINE.search(ln))
    return hits >= max(3, len(lines[:50]) // 4)


def _dedupe_lines(text: str) -> tuple[str, bool]:
    if not text:
        return text, False
    lines = text.splitlines()
    out: list[str] = []
    prev = None
    count = 0
    changed = False
    for ln in lines:
        if ln == prev:
            count += 1
            continue
        if prev is not None and count > 1:
            out.append(f"[ariadne: previous line repeated x{count}]")
            changed = True
        out.append(ln)
        prev = ln
        count = 1
    if prev is not None and count > 1:
        out.append(f"[ariadne: previous line repeated x{count}]")
        changed = True
    return "\n".join(out) + ("\n" if text.endswith("\n") else ""), changed


def _head_tail(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text, False
    head_n = max(limit // 2, 1)
    tail_n = max(limit - head_n, 1)
    head = raw[:head_n]
    tail = raw[-tail_n:]
    marker = b"\n[ariadne: output truncated; kept head+tail]\n"
    return (head + marker + tail).decode("utf-8", errors="replace"), True
