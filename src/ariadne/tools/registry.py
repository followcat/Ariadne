from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..errors import AriadneError, app_error
from ..memory.curated import CuratedStore
from ..memory.facade import MemoryFacade
from ..memory.state import ConversationStateStore
from ..sandbox.port import SandboxExecRequest, SandboxSession
from ..skills.store import SkillStore
from .exposure import ToolExposureState

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]
ToolExposure = Literal["eager", "named_deferred", "hidden"]


@dataclass(slots=True)
class ToolContext:
    session_id: str
    turn_id: str
    sandbox: SandboxSession | None
    memory: MemoryFacade | None = None
    skills: SkillStore | None = None
    exposure: ToolExposureState | None = None
    skill_events: list[Any] | None = None
    evidence_text: str = ""


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    catalog_description: str = ""
    tool_exposure: ToolExposure = "eager"

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def catalog_text(self) -> str:
        lines = []
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden":
                continue
            phrase = spec.catalog_description or spec.description.split(".")[0]
            marker = " (deferred)" if spec.tool_exposure == "named_deferred" else ""
            lines.append(f"- {spec.name}: {phrase}{marker}")
        return "\n".join(lines)

    def build_exposure(self, *, prefer_deferred: bool = True) -> ToolExposureState:
        request: list[dict[str, Any]] = []
        deferred: dict[str, dict[str, Any]] = {}
        callable_names: set[str] = set()
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden":
                continue
            schema = spec.openai_tool()
            if prefer_deferred and spec.tool_exposure == "named_deferred":
                deferred[spec.name] = schema
            else:
                request.append(schema)
                callable_names.add(spec.name)
        # tool_search always eager if present
        if "tool_search" in self.tools and not any(
            (t.get("function") or {}).get("name") == "tool_search" for t in request
        ):
            request.append(self.tools["tool_search"].openai_tool())
            callable_names.add("tool_search")
        # deferred tools still callable after tool_search; mark search tool always callable
        for name in deferred:
            # not callable until loaded
            pass
        return ToolExposureState(
            request_tools=request,
            deferred_tools=deferred,
            callable_function_names=callable_names,
        )

    def list_openai_tools(self) -> list[dict[str, Any]]:
        # eager-only convenience
        return self.build_exposure(prefer_deferred=False).request_tools

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self.tools.get(name)
        if spec is None:
            raise AriadneError(app_error("ARIADNE_UNKNOWN_TOOL", f"Unknown tool: {name}", name=name))
        if ctx.exposure is not None and name not in ctx.exposure.callable_function_names:
            # allow tool_search always if registered
            if name != "tool_search":
                raise AriadneError(
                    app_error(
                        "ARIADNE_UNKNOWN_TOOL",
                        f"Tool not currently callable (deferred/unloaded): {name}",
                        name=name,
                    )
                )
        return await spec.handler(arguments, ctx)


def dumps_tool_output(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def build_default_registry(
    *,
    memory: MemoryFacade | None = None,
    skills: SkillStore | None = None,
    enable_deferred_demo: bool = True,
) -> ToolRegistry:
    registry = ToolRegistry()

    async def sandbox_exec(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.sandbox is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "cmd is required"))
        cwd = str(args.get("cwd") or "/workspace")
        timeout = args.get("timeout_seconds")
        timeout_f = float(timeout) if timeout is not None else 60.0
        result = await ctx.sandbox.exec(
            SandboxExecRequest(cmd=cmd, cwd=cwd, timeout_seconds=timeout_f)
        )
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
            "cwd": result.cwd,
        }

    registry.register(
        ToolSpec(
            name="sandbox_exec",
            catalog_description="run shell command in workspace",
            description=(
                "Run a shell command in the local project sandbox. "
                "Default cwd is the project root (/workspace). Prefer relative paths (e.g. NOTES.md). "
                "Use cwd='/session' for ephemeral scratch ($ARIADNE_SESSION_DIR). "
                "Shell state (cd/export) does not persist across calls; write files to persist. "
                "Prefer non-interactive commands. Large outputs may be truncated with markers."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to execute."},
                    "cwd": {
                        "type": "string",
                        "description": "Virtual cwd: /workspace or /session (default /workspace).",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Timeout seconds (default 60).",
                    },
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
            handler=sandbox_exec,
            tool_exposure="eager",
        )
    )

    async def memory_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.memory is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "memory facade not configured"))
        action = str(args.get("action") or "read")
        return ctx.memory.curated.apply(
            action=action,
            content=str(args.get("content") or ""),
            entry_ref=str(args.get("entry_ref") or ""),
            scope=str(args.get("scope") or "user"),
            session_id=ctx.session_id,
        )

    registry.register(
        ToolSpec(
            name="memory",
            catalog_description="durable curated memory",
            description=(
                "Manage durable curated memory entries (add/update/remove/read). "
                "Use for long-lived preferences and standing instructions. "
                "Do NOT store ephemeral todos or temporary entity fields here when conversation state is available; "
                "use conversation_state for current-session truth. "
                "Capacity is hard-limited; full store returns a structured error."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "update", "remove", "read"],
                        "description": "Memory action.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Entry text for add/update.",
                    },
                    "entry_ref": {
                        "type": "string",
                        "description": "Entry id or 1-based index for update/remove.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["user", "session"],
                        "description": "user=cross-session, session=this session only.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=memory_tool,
            tool_exposure="eager",
        )
    )

    async def conversation_state_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.memory is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "memory facade not configured"))
        action = str(args.get("action") or "read").lower()
        if action == "read":
            text, count = ctx.memory.state.render(ctx.session_id)
            return {"action": "read", "entity_count": count, "text": text, "state": ctx.memory.state.get(ctx.session_id)}
        if action == "apply":
            ops = args.get("operations") or []
            if not isinstance(ops, list):
                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "operations must be a list"))
            evidence = str(args.get("evidence_text") or ctx.evidence_text or "")
            return ctx.memory.state.apply_ops(
                session_id=ctx.session_id,
                operations=ops,
                source_turn_id=ctx.turn_id,
                evidence_text=evidence,
            )
        raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "action must be read|apply"))

    registry.register(
        ToolSpec(
            name="conversation_state",
            catalog_description="authoritative current session state",
            description=(
                "Read or apply closed-set operations to authoritative conversation state. "
                "apply operations each require evidence_quote found in the turn text. "
                "Allowed ops: ensure_entity, set_alias, set_attribute, set_status, "
                "ensure_collection, collection_append, collection_remove."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "apply"]},
                    "operations": {
                        "type": "array",
                        "description": "State operations for apply.",
                        "items": {"type": "object"},
                    },
                    "evidence_text": {
                        "type": "string",
                        "description": "Text that must contain each evidence_quote (defaults to turn text).",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=conversation_state_tool,
            tool_exposure="eager",
        )
    )

    async def search_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill store not configured"))
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 5)
        hits = ctx.skills.search(query, limit=max(1, min(limit, 20)))
        if ctx.skill_events is not None:
            from ..types import SkillEvent

            ctx.skill_events.append(SkillEvent(kind="search", detail=query))
        return {
            "query": query,
            "results": [
                {
                    "name": s.name,
                    "description": s.description,
                    "keywords": s.keywords,
                    "requires_tools": s.requires_tools,
                }
                for s in hits
            ],
        }

    registry.register(
        ToolSpec(
            name="search_skills",
            catalog_description="search installed skills",
            description="Search installed procedural skills by query. Returns short metadata only.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_skills,
            tool_exposure="eager",
        )
    )

    async def load_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill store not configured"))
        name = str(args.get("name") or "").strip()
        skill = ctx.skills.get(name)
        if skill is None:
            raise AriadneError(app_error("ARIADNE_SKILL_NOT_FOUND", f"skill not found: {name}", name=name))
        include_refs = bool(args.get("include_references") or False)
        if ctx.skill_events is not None:
            from ..types import SkillEvent

            ctx.skill_events.append(SkillEvent(kind="load", skill_name=name))
        payload: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
            "requires_tools": skill.requires_tools,
        }
        if include_refs:
            payload["references"] = skill.references
        return payload

    registry.register(
        ToolSpec(
            name="load_skill",
            catalog_description="load full skill body",
            description=(
                "Load a skill body for this turn (tool result scope). "
                "Call after search_skills or when skill index names a needed skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "include_references": {"type": "boolean"},
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=load_skill,
            tool_exposure="eager",
        )
    )

    async def tool_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.exposure is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "exposure state missing"))
        names = args.get("tool_names") or []
        if not isinstance(names, list) or not names:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "tool_names required"))
        loaded = ctx.exposure.load_exact([str(x) for x in names])
        return {
            "loaded": [(t.get("function") or {}).get("name") for t in loaded],
            "still_deferred": sorted(ctx.exposure.deferred_tools.keys() - ctx.exposure.loaded_tool_names),
        }

    registry.register(
        ToolSpec(
            name="tool_search",
            catalog_description="load deferred tool schemas",
            description="Load full schemas for deferred tools by exact name before calling them.",
            parameters={
                "type": "object",
                "properties": {
                    "tool_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                    }
                },
                "required": ["tool_names"],
                "additionalProperties": False,
            },
            handler=tool_search,
            tool_exposure="eager",
        )
    )

    # Example deferred tool for schema-efficiency path
    if enable_deferred_demo:
        async def echo_note(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
            return {"note": str(args.get("note") or "")}

        registry.register(
            ToolSpec(
                name="echo_note",
                catalog_description="echo a short note (deferred demo)",
                description="Deferred demo tool that echoes a note. Load via tool_search first when deferred.",
                parameters={
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                    "additionalProperties": False,
                },
                handler=echo_note,
                tool_exposure="named_deferred",
            )
        )

    return registry
