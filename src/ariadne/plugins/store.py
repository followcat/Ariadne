"""Plugin enablement + credential store (data_dir/plugins.json, mode 0600)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

_SECRET_KEY_RE = re.compile(r"(key|token|password|secret|passwd)", re.I)


def is_secret_config_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def mask_secret_value(value: str) -> str:
    """Show head/tail with ***** in the middle (never return the full secret)."""
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 4:
        return "*****"
    if len(s) <= 8:
        return f"{s[:1]}*****{s[-1:]}"
    return f"{s[:3]}*****{s[-3:]}"


def looks_masked_value(value: str) -> bool:
    return "*****" in str(value or "")


def display_config(config: dict[str, str] | None) -> dict[str, str]:
    """Public-safe config: secrets masked, non-secrets plain."""
    out: dict[str, str] = {}
    for key, raw in (config or {}).items():
        text = str(raw or "")
        if not text:
            continue
        out[key] = mask_secret_value(text) if is_secret_config_key(key) else text
    return out


@dataclass
class PluginStore:
    path: Path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"plugins": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path, 0o600)

    def enable(self, name: str, config: dict[str, str]) -> None:
        data = self._read()
        data.setdefault("plugins", {})[name] = {"enabled": True, "config": dict(config)}
        self._write(data)

    def disable(self, name: str) -> None:
        data = self._read()
        entry = (data.get("plugins") or {}).get(name)
        if entry is None or not entry.get("enabled"):
            raise AriadneError(
                app_error("ARIADNE_PLUGIN_ERROR", f"plugin not enabled: {name}")
            )
        entry["enabled"] = False
        self._write(data)

    def enabled(self) -> dict[str, dict[str, str]]:
        data = self._read()
        return {
            name: dict(entry.get("config") or {})
            for name, entry in (data.get("plugins") or {}).items()
            if entry.get("enabled")
        }

    def list(self) -> dict[str, dict[str, Any]]:
        data = self._read()
        return dict(data.get("plugins") or {})
