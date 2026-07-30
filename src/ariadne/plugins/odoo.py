"""Odoo plugin: JSON-RPC execute_kw search_read + generic call."""

from __future__ import annotations

from typing import Any

from ..tools.registry import ToolContext, ToolSpec
from .base import PLUGIN_REGISTRY, http_json


class OdooPlugin:
    name = "odoo"
    description = "Odoo JSON-RPC (search_read records, generic execute_kw)"
    required_config = ("url", "database", "login", "password")

    def build_tools(self, config: dict[str, str]) -> list[ToolSpec]:
        url = config["url"].rstrip("/") + "/jsonrpc"
        db = config["database"]
        login = config["login"]
        password = config["password"]
        _uid: dict[str, int] = {}

        def _jsonrpc(service: str, method: str, args: list[Any]) -> Any:
            payload = {
                "jsonrpc": "2.0",
                "method": "call",
                "params": {"service": service, "method": method, "args": args},
                "id": 1,
            }
            result = http_json("POST", url, payload=payload)
            if isinstance(result, dict) and result.get("error"):
                from .base import plugin_error

                raise plugin_error(
                    f"odoo rpc error: {str(result['error'])[:300]}", plugin="odoo"
                )
            return result.get("result") if isinstance(result, dict) else result

        def _authenticate() -> int:
            if "uid" not in _uid:
                uid = _jsonrpc("common", "authenticate", [db, login, password, {}])
                if not uid:
                    from .base import plugin_error

                    raise plugin_error("odoo authentication failed", plugin="odoo")
                _uid["uid"] = int(uid)
            return _uid["uid"]

        async def odoo_search_read(args: dict[str, Any], ctx: ToolContext) -> Any:
            model = str(args.get("model") or "").strip()
            if not model:
                from ..errors import AriadneError, app_error

                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "model is required"))
            uid = _authenticate()
            kwargs: dict[str, Any] = {"limit": int(args.get("limit") or 20)}
            fields = args.get("fields")
            if fields:
                kwargs["fields"] = [str(f) for f in fields]
            domain = args.get("domain") or []
            return _jsonrpc(
                "object", "execute_kw", [db, uid, password, model, "search_read", domain, kwargs]
            )

        async def odoo_execute(args: dict[str, Any], ctx: ToolContext) -> Any:
            model = str(args.get("model") or "").strip()
            method = str(args.get("method") or "").strip()
            if not model or not method:
                from ..errors import AriadneError, app_error

                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "model and method are required")
                )
            uid = _authenticate()
            positional = args.get("args") or []
            kwargs = args.get("kwargs") or {}
            return _jsonrpc(
                "object",
                "execute_kw",
                [db, uid, password, model, method, positional, kwargs],
            )

        return [
            ToolSpec(
                name="odoo_search_read",
                catalog_description="search+read Odoo records",
                description=(
                    "search_read on an Odoo model via JSON-RPC. domain uses Odoo "
                    "domain syntax (list of triples). Credentials come from the "
                    "plugin configuration."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "domain": {"type": "array"},
                        "fields": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                    },
                    "required": ["model"],
                    "additionalProperties": False,
                },
                handler=odoo_search_read,
                required_credentials=("odoo.login", "odoo.password"),
                side_effect_level="read",
                network_access="outbound",
                idempotent=True,
            ),
            ToolSpec(
                name="odoo_execute",
                catalog_description="call an Odoo model method",
                description="Generic execute_kw on an Odoo model with positional args and kwargs.",
                parameters={
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "method": {"type": "string"},
                        "args": {"type": "array"},
                        "kwargs": {"type": "object"},
                    },
                    "required": ["model", "method"],
                    "additionalProperties": False,
                },
                handler=odoo_execute,
                required_credentials=("odoo.login", "odoo.password"),
                side_effect_level="unknown",
                network_access="outbound",
                idempotent=None,
            ),
        ]


PLUGIN_REGISTRY["odoo"] = OdooPlugin()
