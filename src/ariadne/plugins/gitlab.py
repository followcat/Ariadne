"""GitLab plugin: generic REST request + merge-request listing."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from ..tools.registry import ToolContext, ToolSpec
from .base import PLUGIN_REGISTRY, http_json


class GitLabPlugin:
    name = "gitlab"
    description = "GitLab REST API (projects, merge requests, generic GET/POST)"
    required_config = ("url", "token")

    def build_tools(self, config: dict[str, str]) -> list[ToolSpec]:
        base = config["url"].rstrip("/") + "/api/v4"
        headers = {"PRIVATE-TOKEN": config["token"]}

        async def gitlab_request(args: dict[str, Any], ctx: ToolContext) -> Any:
            method = str(args.get("method") or "GET").upper()
            path = str(args.get("path") or "").lstrip("/")
            if not path:
                from ..errors import AriadneError, app_error

                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "path is required"))
            query = args.get("query") or {}
            qs = f"?{urlencode(query)}" if query else ""
            body = args.get("body")
            return http_json(method, f"{base}/{path}{qs}", headers=headers, payload=body)

        async def gitlab_list_merge_requests(args: dict[str, Any], ctx: ToolContext) -> Any:
            project = str(args.get("project") or "")
            if not project:
                from ..errors import AriadneError, app_error

                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "project is required"))
            state = str(args.get("state") or "opened")
            qs = urlencode({"state": state, "per_page": int(args.get("per_page") or 20)})
            from urllib.parse import quote

            return http_json(
                "GET",
                f"{base}/projects/{quote(project, safe='')}/merge_requests?{qs}",
                headers=headers,
            )

        return [
            ToolSpec(
                name="gitlab_request",
                catalog_description="call GitLab REST API",
                description=(
                    "Generic GitLab REST call (API v4). path is relative, e.g. "
                    "'projects/123/issues' (URL-encode ids yourself or use "
                    "gitlab_list_merge_requests for MRs). Auth comes from the "
                    "plugin's configured token."
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
                handler=gitlab_request,
            ),
            ToolSpec(
                name="gitlab_list_merge_requests",
                catalog_description="list merge requests of a project",
                description=(
                    "List merge requests for a GitLab project. project is the "
                    "numeric id or URL-encoded path (group%2Fproject)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {"type": "string"},
                        "state": {"type": "string", "enum": ["opened", "closed", "merged", "all"]},
                        "per_page": {"type": "integer"},
                    },
                    "required": ["project"],
                    "additionalProperties": False,
                },
                handler=gitlab_list_merge_requests,
            ),
        ]


PLUGIN_REGISTRY["gitlab"] = GitLabPlugin()
