"""In-process command policy: allowlist/denylist, redaction, audit (personal)."""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Dangerous patterns denied even when allowlist is permissive
DEFAULT_DENY: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs*",
    "dd if=*",
    ":(){ :|:& };:",
    "shutdown*",
    "reboot*",
    "chmod -R 777 /",
)

DEFAULT_REDACT: tuple[tuple[str, str], ...] = (
    (r"(?i)(sk-[a-z0-9]{20,})", r"sk-***"),
    (r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1***"),
    (r"(?i)(authorization:\s*bearer\s+)\S+", r"\1***"),
)


@dataclass
class CommandPolicy:
    """Shell command gate for sandbox_exec / RuntimeAgent."""

    allowed: tuple[str, ...] = ("*",)  # personal default: permissive
    denied: tuple[str, ...] = DEFAULT_DENY
    redaction_patterns: tuple[tuple[str, str], ...] = DEFAULT_REDACT
    audit_path: Path | None = None
    enabled: bool = True

    def is_allowed(self, cmd: str) -> tuple[bool, str]:
        text = (cmd or "").strip()
        if not text:
            return False, "empty command"
        if not self.enabled:
            return True, "policy disabled"
        low = text.lower()
        for pat in self.denied:
            if fnmatch.fnmatch(text, pat) or fnmatch.fnmatch(low, pat.lower()):
                return False, f"denied by pattern {pat!r}"
            if pat.lower() in low and "*" not in pat:
                return False, f"denied by substring {pat!r}"
        if self.allowed == ("*",) or "*" in self.allowed:
            return True, "allow"
        for pat in self.allowed:
            if fnmatch.fnmatch(text, pat) or fnmatch.fnmatch(low, pat.lower()):
                return True, "allow"
        return False, "not in allowlist"

    def redact(self, text: str) -> str:
        out = text or ""
        for pat, repl in self.redaction_patterns:
            out = re.sub(pat, repl, out)
        return out

    def audit(self, record: dict[str, Any]) -> None:
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), **record}
        with self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


@dataclass
class EgressPolicy:
    """Host-side outbound URL policy (web_fetch). Default deny unless allowlist set."""

    allowed_hosts: tuple[str, ...] = ()
    denied_hosts: tuple[str, ...] = ()
    default_allow: bool = False

    def check_url(self, url: str) -> tuple[bool, str]:
        from urllib.parse import urlparse

        raw = (url or "").strip()
        if not raw:
            return False, "empty url"
        try:
            parsed = urlparse(raw)
        except Exception:
            return False, "invalid url"
        host = (parsed.hostname or "").lower()
        if not host:
            return False, "missing host"
        for d in self.denied_hosts:
            if host == d.lower() or host.endswith("." + d.lower()):
                return False, f"host denied: {host}"
        if self.default_allow and not self.allowed_hosts:
            return True, "default allow"
        for a in self.allowed_hosts:
            if host == a.lower() or host.endswith("." + a.lower()) or fnmatch.fnmatch(host, a.lower()):
                return True, "allowlist"
        if not self.allowed_hosts and not self.default_allow:
            return False, "egress default deny (set ARIADNE_EGRESS_ALLOWED)"
        return False, f"host not allowed: {host}"
