from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from ..errors import AriadneError, app_error
from ..memory.facade import MemoryFacade
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
    """CapabilitySpec (TOOLCALL §2.1): one unit of registration."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    catalog_description: str = ""
    tool_exposure: ToolExposure = "eager"
    title: str = ""
    kind: str = "tool"  # tool | system_action | ...
    exposed_to_llm: bool = True
    required_credentials: tuple[str, ...] = ()  # personal v1 ignores credentials

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def schema_chars(self) -> int:
        return len(json.dumps(self.openai_tool(), ensure_ascii=False, separators=(",", ":")))


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec, handler: ToolHandler | None = None) -> None:
        if handler is not None:
            spec.handler = handler
        self.tools[spec.name] = spec

    @classmethod
    def builtins(
        cls,
        *,
        include_sandbox: bool = True,
        memory: MemoryFacade | None = None,
        skills: SkillStore | None = None,
        enable_deferred_demo: bool = True,
    ) -> "ToolRegistry":
        registry = build_default_registry(
            memory=memory, skills=skills, enable_deferred_demo=enable_deferred_demo
        )
        if not include_sandbox:
            registry.tools.pop("sandbox_exec", None)
        return registry

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def catalog_text(self) -> str:
        lines = []
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden" or not spec.exposed_to_llm:
                continue
            phrase = spec.catalog_description or spec.description.split(".")[0]
            marker = " (deferred)" if spec.tool_exposure == "named_deferred" else ""
            lines.append(f"- {spec.name}: {phrase}{marker}")
        return "\n".join(lines)

    def catalog_chars(self) -> int:
        return len(self.catalog_text())

    def build_exposure(self, *, prefer_deferred: bool = True) -> ToolExposureState:
        request: list[dict[str, Any]] = []
        deferred: dict[str, dict[str, Any]] = {}
        callable_names: set[str] = set()
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden" or not spec.exposed_to_llm:
                continue
            schema = spec.openai_tool()
            if prefer_deferred and spec.tool_exposure == "named_deferred":
                deferred[spec.name] = schema
            else:
                request.append(schema)
                callable_names.add(spec.name)
        if "tool_search" in self.tools and not any(
            (t.get("function") or {}).get("name") == "tool_search" for t in request
        ):
            request.append(self.tools["tool_search"].openai_tool())
            callable_names.add("tool_search")
        return ToolExposureState(
            request_tools=request,
            deferred_tools=deferred,
            callable_function_names=callable_names,
        )

    def list_openai_tools(self) -> list[dict[str, Any]]:
        return self.build_exposure(prefer_deferred=False).request_tools

    def schema_chars_for(self, tools: list[dict[str, Any]]) -> int:
        return len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self.tools.get(name)
        if spec is None:
            raise AriadneError(app_error("ARIADNE_UNKNOWN_TOOL", f"Unknown tool: {name}", name=name))
        if ctx.exposure is not None and name not in ctx.exposure.callable_function_names:
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
            "compressed": result.compressed,
            "duration_ms": result.duration_ms,
            "cwd": result.cwd,
        }

    registry.register(
        ToolSpec(
            name="sandbox_exec",
            catalog_description="run shell command in workspace",
            description=(
                "Run a shell command in the local project sandbox. "
                "Default cwd is the project root (/workspace). Prefer relative paths. "
                "Use cwd='/session' for ephemeral scratch. "
                "Shell state does not persist across calls."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "cwd": {"type": "string"},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
            handler=sandbox_exec,
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
                "Manage durable curated memory (add/update/remove/read). "
                "Use for long-lived preferences. Use conversation_state for current-session truth."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "remove", "read"]},
                    "content": {"type": "string"},
                    "entry_ref": {"type": "string"},
                    "scope": {"type": "string", "enum": ["user", "session"]},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=memory_tool,
        )
    )

    async def conversation_state_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.memory is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "memory facade not configured"))
        action = str(args.get("action") or "read").lower()
        if action == "read":
            text, count = ctx.memory.state.render(ctx.session_id)
            return {
                "action": "read",
                "entity_count": count,
                "text": text,
                "state": ctx.memory.state.get(ctx.session_id),
            }
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
                "Read or apply conversation state (L2 authoritative session state: "
                "todos, entities, current facts). "
                "action=read returns the rendered state. "
                "action=apply takes operations=[...]; every op must include an "
                "evidence_quote copied verbatim from the conversation text. "
                "Allowed ops: "
                "ensure_entity {entity_id, type?}; "
                "set_alias {entity_id, alias}; "
                "set_attribute {entity_id, key, value, authority?}; "
                "set_status {entity_id, status: active|done|cancelled|archived}; "
                "set_relation {relation, from, to}; "
                "remove_relation {relation, from, to}; "
                "ensure_collection {name}; "
                "collection_append {name, member}; "
                "collection_remove {name, member}; "
                "collection_move {name, member, to_index}. "
                "For durable cross-session preferences use the memory tool instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "apply"]},
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "ensure_entity",
                                        "set_alias",
                                        "set_attribute",
                                        "set_status",
                                        "set_relation",
                                        "remove_relation",
                                        "ensure_collection",
                                        "collection_append",
                                        "collection_remove",
                                        "collection_move",
                                    ],
                                },
                                "entity_id": {"type": "string"},
                                "type": {"type": "string"},
                                "alias": {"type": "string"},
                                "key": {"type": "string"},
                                "value": {},
                                "authority": {"type": "string"},
                                "status": {"type": "string"},
                                "relation": {"type": "string"},
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                                "name": {"type": "string"},
                                "member": {"type": "string"},
                                "to_index": {"type": "integer"},
                                "evidence_quote": {
                                    "type": "string",
                                    "description": "verbatim quote from the conversation justifying this op",
                                },
                            },
                            "required": ["op", "evidence_quote"],
                        },
                    },
                    "evidence_text": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=conversation_state_tool,
        )
    )

    async def search_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill store not configured"))
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 5)
        mode = str(args.get("mode") or "lexical").lower()
        if mode == "hybrid":
            hits = await ctx.skills.search_hybrid(query, limit=max(1, min(limit, 20)))
            scored_hits = [(None, s) for s in hits]
        else:
            scored_hits = ctx.skills.search_scored(query, limit=max(1, min(limit, 20)))
        if ctx.skill_events is not None:
            from ..types import SkillEvent

            ctx.skill_events.append(SkillEvent(kind="search", detail=f"{mode}:{query}"))
        return {
            "query": query,
            "mode": mode,
            "results": [
                {
                    "name": s.name,
                    "description": s.description,
                    "keywords": s.keywords,
                    "requires_tools": s.requires_tools,
                    "namespace": s.namespace,
                    **({"score": score} if score is not None else {}),
                }
                for score, s in scored_hits
            ],
        }

    registry.register(
        ToolSpec(
            name="search_skills",
            catalog_description="search installed skills",
            description="Search skills. mode=lexical|hybrid (hybrid blends lexical + embeddings).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "mode": {"type": "string", "enum": ["lexical", "hybrid"]},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=search_skills,
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
            "namespace": skill.namespace,
            "version": skill.version,
        }
        if include_refs:
            payload["references"] = skill.references
        return payload

    registry.register(
        ToolSpec(
            name="load_skill",
            catalog_description="load full skill body",
            description="Load skill body for this turn (tool result scope).",
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
        )
    )

    async def skill_manage(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill store not configured"))
        action = str(args.get("action") or "").lower()
        name = str(args.get("name") or "")
        keywords = args.get("keywords") or []
        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        return ctx.skills.manage(
            action=action,
            name=name,
            description=str(args.get("description") or ""),
            body=str(args.get("body") or ""),
            keywords=[str(x) for x in keywords],
        )

    registry.register(
        ToolSpec(
            name="skill_manage",
            catalog_description="create/update user skills",
            description="Create, update, or delete versioned user skills under the user skills root.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "name"],
                "additionalProperties": False,
            },
            handler=skill_manage,
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
            "schema_chars_loaded": len(json.dumps(loaded, ensure_ascii=False, separators=(",", ":"))),
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
        )
    )

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
