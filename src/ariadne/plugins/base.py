"""Plugin protocol + shared HTTP helpers."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import AriadneError, app_error
from ..tools.registry import ToolSpec


class Plugin(Protocol):
    name: str
    description: str
    required_config: tuple[str, ...]  # e.g. ("url", "token")

    def build_tools(self, config: dict[str, str]) -> list[ToolSpec]: ...


def plugin_error(message: str, **details: object) -> AriadneError:
    return AriadneError(app_error("ARIADNE_PLUGIN_ERROR", message, **details))


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: Any = None,
    timeout: float = 30.0,
) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise plugin_error(f"HTTP {exc.code} from {url}: {detail}", url=url) from exc
    except urllib.error.URLError as exc:
        raise plugin_error(f"connection failed for {url}: {exc.reason}", url=url) from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def require_config(plugin: str, config: dict[str, str], keys: tuple[str, ...]) -> None:
    missing = [k for k in keys if not str(config.get(k) or "").strip()]
    if missing:
        raise plugin_error(f"plugin {plugin} missing config: {', '.join(missing)}", plugin=plugin)


@dataclass(slots=True)
class PluginToolDefaults:
    timeout: float = 30.0


# filled by the odoo/gitlab/redmine modules at import time
PLUGIN_REGISTRY: dict[str, Plugin] = {}


def build_plugin_tools(name: str, config: dict[str, str]) -> list[ToolSpec]:
    plugin = PLUGIN_REGISTRY.get(name)
    if plugin is None:
        raise plugin_error(f"unknown plugin: {name!r}", plugin=name)
    require_config(name, config, plugin.required_config)
    return plugin.build_tools(config)


from . import gitlab, odoo, redmine  # noqa: E402,F401  (registers plugins)
