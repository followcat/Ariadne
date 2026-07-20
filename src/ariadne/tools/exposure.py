from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoadExactResult:
    """Result of materializing deferred tool schemas."""

    loaded: list[dict[str, Any]] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    already_loaded: list[str] = field(default_factory=list)

    def loaded_names(self) -> list[str]:
        names: list[str] = []
        for t in self.loaded:
            n = (t.get("function") or {}).get("name") or t.get("name")
            if n:
                names.append(str(n))
        return names


@dataclass
class ToolExposureState:
    """Tracks eager vs deferred tool schemas for one turn/loop."""

    request_tools: list[dict[str, Any]]
    deferred_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    callable_function_names: set[str] = field(default_factory=set)
    loaded_tool_names: set[str] = field(default_factory=set)
    # none | function (tool_search) | native (provider-side search)
    client_search_mode: str = "function"
    # Optional session visibility filter applied at build time (names allowed)
    session_visible: set[str] | None = None

    def load_exact(self, tool_names: list[str]) -> list[dict[str, Any]]:
        """Materialize deferred schemas. Prefer :meth:`load_exact_report` for not_found."""
        return self.load_exact_report(tool_names).loaded

    def load_exact_report(self, tool_names: list[str]) -> LoadExactResult:
        """Materialize deferred schemas; report not_found and already_loaded."""
        result = LoadExactResult()
        seen: set[str] = set()
        for raw in tool_names[:5]:
            name = str(raw or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if name in self.loaded_tool_names and name in self.callable_function_names:
                result.already_loaded.append(name)
                # still include schema if present for idempotent responses
                schema = self.deferred_tools.get(name)
                if schema is not None and not any(
                    (t.get("function") or {}).get("name") == name for t in result.loaded
                ):
                    # already on wire; skip duplicating in loaded list
                    pass
                continue
            schema = self.deferred_tools.get(name)
            if schema is None:
                # Not deferred (or unknown). Eager tools are already callable.
                if name in self.callable_function_names:
                    result.already_loaded.append(name)
                else:
                    result.not_found.append(name)
                continue
            result.loaded.append(dict(schema))
            self.loaded_tool_names.add(name)
            self.callable_function_names.add(name)
            if not any(
                (t.get("function") or {}).get("name") == name or t.get("name") == name
                for t in self.request_tools
            ):
                self.request_tools.append(dict(schema))
        return result
