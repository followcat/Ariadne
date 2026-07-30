"""Redmine plugin: issues listing + generic REST request."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..tools.registry import ToolContext, ToolSpec
from .base import PLUGIN_REGISTRY, http_json


class RedminePlugin:
    name = "redmine"
    description = "Redmine REST API (issues, generic GET/POST/PUT)"
    required_config = ("url", "api_key")

    def build_tools(self, config: dict[str, str]) -> list[ToolSpec]:
        base = config["url"].rstrip("/")
        headers = {"X-Redmine-API-Key": config["api_key"]}

        async def redmine_list_issues(args: dict[str, Any], ctx: ToolContext) -> Any:
            query: dict[str, Any] = {"limit": int(args.get("limit") or 25)}
            for key in ("project_id", "status_id", "assigned_to_id"):
                if args.get(key) is not None:
                    query[key] = args[key]
            qs = urlencode(query)
            return http_json("GET", f"{base}/issues.json?{qs}", headers=headers)

        async def redmine_request(args: dict[str, Any], ctx: ToolContext) -> Any:
            method = str(args.get("method") or "GET").upper()
            path = str(args.get("path") or "").lstrip("/")
            if not path:
                from ..errors import AriadneError, app_error

                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "path is required"))
            if not path.endswith(".json"):
                path += ".json"
            query = args.get("query") or {}
            qs = f"?{urlencode(query)}" if query else ""
            return http_json(
                method, f"{base}/{path}{qs}", headers=headers, payload=args.get("body")
            )

        return [
            ToolSpec(
                name="redmine_list_issues",
                catalog_description="list Redmine issues",
                description=(
                    "List Redmine issues with optional project/status/assignee "
                    "filters. Auth comes from the plugin's configured API key."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project_id": {},
                        "status_id": {},
                        "assigned_to_id": {},
                        "limit": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                handler=redmine_list_issues,
                required_credentials=("redmine.api_key",),
                side_effect_level="read",
                network_access="outbound",
                idempotent=True,
            ),
            ToolSpec(
                name="redmine_request",
                catalog_description="call Redmine REST API",
                description=(
                    "Generic Redmine REST call. path like 'issues/123' (.json is "
                    "added when missing). Auth comes from the plugin's API key."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                        "path": {"type": "string"},
                        "query": {"type": "object"},
                        "body": {},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=redmine_request,
                required_credentials=("redmine.api_key",),
                side_effect_level="unknown",
                side_effect_resolver=lambda args: (
                    "read"
                    if str(args.get("method") or "GET").upper() in {"GET", "HEAD"}
                    else (
                        "destructive"
                        if str(args.get("method") or "GET").upper() == "DELETE"
                        else "write"
                    )
                ),
                network_access="outbound",
                idempotent=None,
            ),
        ]


PLUGIN_REGISTRY["redmine"] = RedminePlugin()
