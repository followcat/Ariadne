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
# host-side approval: called with (tool_name, arguments) before dispatch;
# False denies the invocation (SANDBOX.md: confirmation stays a host concern)
ApprovalHook = Callable[[str, dict[str, Any]], bool]


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
    approval_hook: ApprovalHook | None = None


@dataclass(slots=True)
class ToolSpec:
    """One capability registration unit (TOOLCALL §2.1 CapabilitySpec).

    Naming map (docs CapabilitySpec → fields here):

    - ``description`` (docs catalog phrase) → :attr:`catalog_description`
      (falls back to first sentence of ``description``)
    - ``tool_schema`` (docs full callable schema) → :meth:`tool_schema` /
      :meth:`openai_tool` built from ``name`` + ``description`` + ``parameters``
    - long when/how policy lives in ``description`` (schema description)
    """

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

    def catalog_phrase(self) -> str:
        """Short discovery phrase (docs CapabilitySpec.description)."""
        if self.catalog_description.strip():
            return self.catalog_description.strip()
        return (self.description or "").split(".")[0].strip() or self.name

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def tool_schema(self) -> dict[str, Any]:
        """Full callable schema for the model API (docs CapabilitySpec.tool_schema)."""
        return self.openai_tool()

    def schema_chars(self) -> int:
        return len(json.dumps(self.tool_schema(), ensure_ascii=False, separators=(",", ":")))


# Public alias aligned with TOOLCALL / ARCHITECTURE naming.
CapabilitySpec = ToolSpec


@dataclass
class ToolRegistry:
    tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(
        self, spec: ToolSpec, handler: ToolHandler | None = None, *, replace: bool = False
    ) -> None:
        if handler is not None:
            spec.handler = handler
        if spec.name in self.tools and not replace:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    f"duplicate tool registration: {spec.name!r} (pass replace=True to override)",
                    name=spec.name,
                )
            )
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
            for name in [n for n in registry.tools if n.startswith("sandbox_")]:
                registry.tools.pop(name, None)
        return registry

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def catalog_text(self, *, session_visible: set[str] | None = None) -> str:
        lines = []
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden" or not spec.exposed_to_llm:
                continue
            if session_visible is not None and spec.name not in session_visible:
                continue
            phrase = spec.catalog_phrase()
            marker = " (deferred)" if spec.tool_exposure == "named_deferred" else ""
            title = f" [{spec.title}]" if spec.title and spec.title != spec.name else ""
            lines.append(f"- {spec.name}: {phrase}{marker}{title}")
        return "\n".join(lines)

    def schema_cost_report(self, *, prefer_deferred: bool = True) -> dict[str, Any]:
        """Schema-cost snapshot for evals (TOOLCALL §3.2 / TC-08).

        Correctness and cost must be scored separately: deferred tools remain
        discoverable in the catalog and loadable; cost counts only wire schemas.
        """
        exp = self.build_exposure(prefer_deferred=prefer_deferred)
        request_chars = self.schema_chars_for(exp.request_tools)
        deferred_chars = sum(
            len(json.dumps(s, ensure_ascii=False, separators=(",", ":")))
            for s in exp.deferred_tools.values()
        )
        all_schemas = [
            s.tool_schema()
            for s in self.tools.values()
            if s.exposed_to_llm and s.tool_exposure != "hidden"
        ]
        eager_all_chars = self.schema_chars_for(all_schemas)
        return {
            "prefer_deferred": prefer_deferred,
            "request_tool_count": len(exp.request_tools),
            "deferred_tool_count": len(exp.deferred_tools),
            "callable_count": len(exp.callable_function_names),
            "request_schema_chars": request_chars,
            "deferred_schema_chars": deferred_chars,
            "eager_all_schema_chars": eager_all_chars,
            "catalog_chars": self.catalog_chars(),
            "deferred_names": sorted(exp.deferred_tools.keys()),
            "request_names": sorted(
                (t.get("function") or {}).get("name") or "" for t in exp.request_tools
            ),
        }

    def catalog_chars(self) -> int:
        return len(self.catalog_text())

    def build_exposure(
        self,
        *,
        prefer_deferred: bool = True,
        session_visible: set[str] | None = None,
        client_search_mode: str = "function",
    ) -> ToolExposureState:
        """Build wire exposure for a turn.

        ``session_visible``: optional allow-list of tool names for this session.
        Hidden tools and ``exposed_to_llm=False`` are always excluded. When the
        filter is set, tools outside it are omitted from catalog/wire entirely.

        ``client_search_mode`` (TOOLCALL §2.3):

        - ``function``: offer ``tool_search`` to materialize deferred schemas
        - ``native``: no ``tool_search`` on the wire; host auto-materializes on
          first invoke of a deferred name (provider-native search path)
        - ``none``: deferred tools stay unloadable (eager-only wire)
        """
        mode = (client_search_mode or "function").strip().lower()
        if mode not in {"function", "native", "none"}:
            mode = "function"
        request: list[dict[str, Any]] = []
        deferred: dict[str, dict[str, Any]] = {}
        callable_names: set[str] = set()
        for spec in self.tools.values():
            if spec.tool_exposure == "hidden" or not spec.exposed_to_llm:
                continue
            if session_visible is not None and spec.name not in session_visible:
                if not (spec.name == "tool_search" and mode == "function"):
                    continue
            # In native/none modes, tool_search is never offered as a function.
            if spec.name == "tool_search" and mode != "function":
                continue
            schema = spec.openai_tool()
            if prefer_deferred and spec.tool_exposure == "named_deferred":
                if mode == "none":
                    # Hide deferred entirely when search is disabled.
                    continue
                deferred[spec.name] = schema
            else:
                request.append(schema)
                callable_names.add(spec.name)
        if (
            mode == "function"
            and deferred
            and "tool_search" in self.tools
            and not any(
                (t.get("function") or {}).get("name") == "tool_search" for t in request
            )
        ):
            if session_visible is None or "tool_search" in session_visible:
                request.append(self.tools["tool_search"].openai_tool())
                callable_names.add("tool_search")
        elif (
            mode == "function"
            and "tool_search" in self.tools
            and not any(
                (t.get("function") or {}).get("name") == "tool_search" for t in request
            )
        ):
            if session_visible is None or "tool_search" in session_visible:
                request.append(self.tools["tool_search"].openai_tool())
                callable_names.add("tool_search")
        return ToolExposureState(
            request_tools=request,
            deferred_tools=deferred,
            callable_function_names=callable_names,
            client_search_mode=mode,
            session_visible=set(session_visible) if session_visible is not None else None,
        )

    def ensure_titles(self) -> None:
        """Fill missing title from name (TC title/kind completeness)."""
        for spec in self.tools.values():
            if not (spec.title or "").strip():
                spec.title = spec.name.replace("_", " ").title()
            if not (spec.kind or "").strip():
                spec.kind = "tool"

    def list_openai_tools(self) -> list[dict[str, Any]]:
        return self.build_exposure(prefer_deferred=False).request_tools

    def schema_chars_for(self, tools: list[dict[str, Any]]) -> int:
        return len(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> None:
        """Lightweight JSON-schema subset: required keys + object type (TOOLCALL §4)."""
        spec = self.tools.get(name)
        if spec is None:
            return
        if not isinstance(arguments, dict):
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"tool {name!r} arguments must be a JSON object",
                    name=name,
                )
            )
        params = spec.parameters or {}
        required = params.get("required") or []
        if not isinstance(required, list):
            return
        missing = [k for k in required if k not in arguments]
        if missing:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"tool {name!r} missing required: {', '.join(missing)}",
                    name=name,
                    missing=missing,
                )
            )

    async def invoke(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> Any:
        spec = self.tools.get(name)
        if spec is None:
            raise AriadneError(app_error("ARIADNE_UNKNOWN_TOOL", f"Unknown tool: {name}", name=name))
        if ctx.exposure is not None and name not in ctx.exposure.callable_function_names:
            # Native deferred search: auto-materialize known deferred tools.
            if ctx.exposure.ensure_callable(name):
                pass
            elif name != "tool_search":
                raise AriadneError(
                    app_error(
                        "ARIADNE_UNKNOWN_TOOL",
                        f"Tool not currently callable (deferred/unloaded): {name}",
                        name=name,
                    )
                )
        self.validate_arguments(name, arguments)
        if ctx.approval_hook is not None and not ctx.approval_hook(name, arguments):
            raise AriadneError(
                app_error(
                    "ARIADNE_TOOL_DENIED",
                    f"Invocation denied by host approval policy: {name}",
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
            title="Sandbox exec",
            kind="tool",
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
            title="Memory",
            kind="tool",
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
            # Large schema — design center is deferred (TOOLCALL §3.2).
            tool_exposure="named_deferred",
            title="Conversation state",
            kind="tool",
        )
    )

    async def search_skills(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill store not configured"))
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 5)
        mode = str(args.get("mode") or "lexical").lower()
        if mode == "hybrid":
            scored_hits = await ctx.skills.search_hybrid_scored(
                query, limit=max(1, min(limit, 20))
            )
        else:
            scored_hits = [
                (float(score), s)
                for score, s in ctx.skills.search_scored(query, limit=max(1, min(limit, 20)))
            ]
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
                    "score": round(float(score), 4),
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
            title="Search skills",
            kind="tool",
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
        ref_names = args.get("references")
        targeted: list[str] | None = None
        if isinstance(ref_names, list) and ref_names:
            targeted = [str(x) for x in ref_names]
            include_refs = True
        if ctx.skill_events is not None:
            from ..types import SkillEvent

            ctx.skill_events.append(SkillEvent(kind="load", skill_name=name))
        # requires_tools enforcement: report missing tools (do not invent handlers).
        available = set(registry.tools.keys())
        missing = ctx.skills.missing_tools(skill, available)
        payload: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description,
            "body": skill.body,
            "requires_tools": skill.requires_tools,
            "missing_tools": missing,
            "requires_tools_ok": not missing,
            "namespace": skill.namespace,
            "version": skill.version,
            "tags": skill.tags,
        }
        if include_refs:
            refs = skill.select_references(targeted)
            payload["references"] = refs
            if targeted is not None:
                payload["references_requested"] = targeted
                payload["references_missing"] = [
                    r for r in targeted if r not in refs and f"{r}.md" not in refs
                ]
        if missing:
            payload["warning"] = (
                f"skill requires tools not registered: {', '.join(missing)}"
            )
        return payload

    registry.register(
        ToolSpec(
            name="load_skill",
            catalog_description="load full skill body",
            description=(
                "Load skill body for this turn (tool result scope). "
                "Optional references=[name.md,…] loads only those files; "
                "include_references=true loads all. Reports missing requires_tools."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "include_references": {"type": "boolean"},
                    "references": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Targeted reference filenames to load",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            handler=load_skill,
            title="Load skill",
            kind="tool",
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
            tool_exposure="named_deferred",
            title="Skill manage",
        )
    )

    async def tool_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.exposure is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "exposure state missing"))
        names = args.get("tool_names") or []
        if not isinstance(names, list) or not names:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "tool_names required"))
        report = ctx.exposure.load_exact_report([str(x) for x in names])
        loaded = report.loaded
        return {
            "loaded": report.loaded_names(),
            "not_found": report.not_found,
            "already_loaded": report.already_loaded,
            "still_deferred": sorted(
                ctx.exposure.deferred_tools.keys() - ctx.exposure.loaded_tool_names
            ),
            "schema_chars_loaded": len(
                json.dumps(loaded, ensure_ascii=False, separators=(",", ":"))
            ),
        }

    registry.register(
        ToolSpec(
            name="tool_search",
            catalog_description="load deferred tool schemas",
            description=(
                "Load full schemas for deferred tools by exact name before calling them. "
                "Returns loaded, not_found, and already_loaded lists."
            ),
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
            title="Tool search",
            kind="tool",
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
                title="Echo note",
                kind="tool",
            )
        )

    from .filetools import register_file_tools

    register_file_tools(registry)
    registry.ensure_titles()
    return registry
