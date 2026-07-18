from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolExposureState:
    """Tracks eager vs deferred tool schemas for one turn/loop."""

    request_tools: list[dict[str, Any]]
    deferred_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    callable_function_names: set[str] = field(default_factory=set)
    loaded_tool_names: set[str] = field(default_factory=set)
    # none | function (tool_search) | native (provider-side search)
    client_search_mode: str = "function"

    def load_exact(self, tool_names: list[str]) -> list[dict[str, Any]]:
        loaded: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in tool_names[:5]:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            schema = self.deferred_tools.get(name)
            if schema is None:
                continue
            loaded.append(dict(schema))
            self.loaded_tool_names.add(name)
            self.callable_function_names.add(name)
            # ensure present on request_tools for subsequent model calls
            if not any(
                (t.get("function") or {}).get("name") == name
                or t.get("name") == name
                for t in self.request_tools
            ):
                self.request_tools.append(dict(schema))
        return loaded
