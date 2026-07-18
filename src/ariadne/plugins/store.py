"""Plugin enablement + credential store (data_dir/plugins.json, mode 0600)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error


@dataclass
class PluginStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"plugins": {}})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
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
