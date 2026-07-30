from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from ..errors import AriadneError, app_error
from ..memory.facade import MemoryFacade
from ..sandbox.port import SandboxExecRequest, SandboxSession
from ..skills.store import SkillStore
from .exposure import ToolExposureState

ToolHandler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]
ToolExposure = Literal["eager", "named_deferred", "hidden"]
SideEffectLevel = Literal["none", "read", "write", "destructive", "unknown"]
NetworkAccess = Literal["none", "outbound", "unknown"]
SideEffectResolver = Callable[[dict[str, Any]], SideEffectLevel]
# host-side approval: called with (tool_name, arguments) before dispatch;
# False denies the invocation (SANDBOX.md: confirmation stays a host concern)
ApprovalHook = Callable[[str, dict[str, Any], dict[str, Any]], bool]


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
    # User/tool-observed evidence only; excludes the model's own assertions.
    observed_evidence_text: str = ""
    # Exact current user input. Confirmation-gated memory tools must not infer
    # consent from assistant-authored evidence_text.
    user_text: str = ""
    approval_hook: ApprovalHook | None = None
    runtime_agent: Any | None = None  # sandbox.runtime_agent.RuntimeAgent
    user_id: str | None = None
    available_credentials: frozenset[str] = frozenset()


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
    required_credentials: tuple[str, ...] = ()
    side_effect_level: SideEffectLevel = "unknown"
    network_access: NetworkAccess = "unknown"
    idempotent: bool | None = None
    failure_codes: tuple[str, ...] = ()
    verification_hint: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()
    side_effect_resolver: SideEffectResolver | None = None

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

    def effect_for(self, arguments: dict[str, Any]) -> SideEffectLevel:
        effect = (
            self.side_effect_resolver(arguments)
            if self.side_effect_resolver is not None
            else self.side_effect_level
        )
        if effect not in {"none", "read", "write", "destructive", "unknown"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    f"tool {self.name!r} returned invalid side-effect metadata: {effect!r}",
                    name=self.name,
                )
            )
        return effect

    def approval_metadata(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "side_effect_level": self.effect_for(arguments),
            "network_access": self.network_access,
            "idempotent": self.idempotent,
            "required_credentials": list(self.required_credentials),
            "verification_hint": list(self.verification_hint),
        }


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
        try:
            Draft202012Validator.check_schema(spec.parameters)
        except SchemaError as exc:
            raise AriadneError(
                app_error(
                    "ARIADNE_CONFIG_INVALID",
                    f"invalid JSON Schema for tool {spec.name!r}: {exc.message}",
                    name=spec.name,
                )
            ) from exc
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
            idempotency = (
                "yes" if spec.idempotent is True else "no" if spec.idempotent is False else "unknown"
            )
            planning = (
                f" effect={spec.side_effect_level} network={spec.network_access} "
                f"idempotent={idempotency}"
            )
            lines.append(f"- {spec.name}: {phrase}{marker}{title};{planning}")
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
        """Validate runtime arguments against the complete declared JSON Schema."""
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
        try:
            Draft202012Validator(spec.parameters or {}).validate(arguments)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path)
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"tool {name!r} arguments failed JSON Schema validation: {exc.message}",
                    name=name,
                    path=path,
                    validator=str(exc.validator or ""),
                )
            ) from exc

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
        missing_credentials = sorted(
            set(spec.required_credentials) - set(ctx.available_credentials)
        )
        if missing_credentials:
            raise AriadneError(
                app_error(
                    "ARIADNE_TOOL_CREDENTIALS_MISSING",
                    f"tool {name!r} is missing required credentials",
                    name=name,
                    missing=missing_credentials,
                )
            )
        if ctx.approval_hook is not None and not ctx.approval_hook(
            name, arguments, spec.approval_metadata(arguments)
        ):
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
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "cmd is required"))
        cwd = str(args.get("cwd") or "/workspace")
        timeout = args.get("timeout_seconds")
        timeout_f = float(timeout) if timeout is not None else 60.0
        # Prefer in-process RuntimeAgent (policy + audit); fall back to raw session.
        if ctx.runtime_agent is not None:
            return await ctx.runtime_agent.execute_shell(
                cmd, cwd=cwd, timeout_seconds=timeout_f
            )
        if ctx.sandbox is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox session"))
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
            catalog_description="shell fallback in sandbox (prefer file tools)",
            description=(
                "FALLBACK: run a shell command in the sandbox container. "
                "Prefer sandbox_read_file / sandbox_write_file / sandbox_edit_file for file work, "
                "and web_fetch for HTTP. Default cwd=/workspace; use /session for scratch. "
                "Shell state does not persist across calls. Subject to command policy."
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
            side_effect_level="unknown",
            network_access="none",
            idempotent=None,
            failure_codes=("ARIADNE_SANDBOX_DISABLED", "ARIADNE_TOOL_FAILED"),
            verification_hint=("command_exit",),
        )
    )

    async def web_fetch(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Host-side HTTP GET/POST — container stays network-none (Codex-style)."""
        import httpx

        from ..sandbox.policy import EgressPolicy

        url = str(args.get("url") or "").strip()
        method = str(args.get("method") or "GET").upper()
        timeout = float(args.get("timeout_seconds") or 30)
        if not url:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "url is required"))
        policy: EgressPolicy | None = None
        if ctx.runtime_agent is not None and getattr(ctx.runtime_agent, "egress_policy", None):
            policy = ctx.runtime_agent.egress_policy
        if policy is None:
            policy = EgressPolicy(default_allow=False, allowed_hosts=())
        ok, reason = policy.check_url(url)
        if not ok:
            raise AriadneError(
                app_error("ARIADNE_TOOL_DENIED", f"egress denied: {reason}", url=url)
            )
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
                resp = await client.request(method, url)
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(
                app_error("ARIADNE_TOOL_FAILED", f"web_fetch failed: {exc}", url=url)
            ) from exc
        body = resp.text
        if len(body) > 200_000:
            body = body[:200_000] + "\n[ariadne: truncated]"
        return {
            "url": str(resp.url),
            "status_code": resp.status_code,
            "headers": {k: v for k, v in list(resp.headers.items())[:40]},
            "body": body,
            "egress": reason,
        }

    registry.register(
        ToolSpec(
            name="web_fetch",
            catalog_description="fetch URL from host (egress policy)",
            description=(
                "Fetch a URL on the **host** (not inside the container). "
                "Sandbox stays --network none. Subject to egress allowlist "
                "(ARIADNE_EGRESS_ALLOWED). Prefer this over curl in sandbox_exec."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string"},
                    "timeout_seconds": {"type": "number"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            handler=web_fetch,
            title="Web fetch",
            kind="tool",
            side_effect_level="read",
            side_effect_resolver=lambda args: (
                "read" if str(args.get("method") or "GET").upper() in {"GET", "HEAD"} else "write"
            ),
            network_access="outbound",
            idempotent=None,
            failure_codes=("ARIADNE_TOOL_DENIED", "ARIADNE_TOOL_FAILED"),
        )
    )

    async def memory_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.memory is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "memory facade not configured"))
        action = str(args.get("action") or "read")
        apply = getattr(ctx.memory, "apply_curated", None)
        if callable(apply):
            return apply(
                action=action,
                content=str(args.get("content") or ""),
                entry_ref=str(args.get("entry_ref") or ""),
                scope=str(args.get("scope") or "user"),
                session_id=ctx.session_id,
                source_turn_id=str(getattr(ctx, "turn_id", "") or ""),
                user_id=getattr(ctx, "user_id", None),
            )
        return ctx.memory.curated.apply(
            action=action,
            content=str(args.get("content") or ""),
            entry_ref=str(args.get("entry_ref") or ""),
            scope=str(args.get("scope") or "user"),
            session_id=ctx.session_id,
            source_turn_id=str(getattr(ctx, "turn_id", "") or ""),
        )

    registry.register(
        ToolSpec(
            name="memory",
            catalog_description="durable curated memory",
            title="Memory",
            kind="tool",
            description=(
                "Manage durable curated memory (add/update/remove/read). "
                "Scopes: user (cross-workspace prefs), workspace (project facts), "
                "session (thread-only). "
                "Use for long-lived preferences. Use conversation_state for current-session truth. "
                "For episodic recall of past turns use memory_search."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "remove", "read"]},
                    "content": {"type": "string"},
                    "entry_ref": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["user", "workspace", "session"],
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=memory_tool,
            side_effect_level="unknown",
            side_effect_resolver=lambda args: (
                "read" if str(args.get("action") or "read") == "read" else "write"
            ),
            network_access="none",
            idempotent=None,
        )
    )

    async def memory_search_tool(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.memory is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "memory facade not configured"))
        search = getattr(ctx.memory, "memory_search", None)
        if not callable(search):
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "memory_search not available on facade")
            )
        before = args.get("before_turn_id")
        before_s = str(before) if before not in (None, "") else None
        return await search(
            query=str(args.get("query") or ""),
            session_id=ctx.session_id,
            scope=str(args.get("scope") or "session"),
            mode=str(args.get("mode") or "") or None,
            limit=int(args.get("limit") or 8),
            before_turn_id=before_s,
            user_id=getattr(ctx, "user_id", None),
        )

    registry.register(
        ToolSpec(
            name="memory_search",
            catalog_description="graded episodic memory search",
            title="Memory search",
            kind="tool",
            description=(
                "Search past turns/summaries for a named scope (session|workspace|user). "
                "mode=fast: local lexical+embedding only. "
                "mode=auto: fast then upgrade to deep on weak/vague signals. "
                "mode=deep: alias/query planning plus constrained episode entity/relation/"
                "timeline/decision/outcome traversal and rerank of real candidates only — "
                "never invents history. Hits always carry turn_id and store-grounded snippets. "
                "Use when L0/L2/curated context is insufficient; do not dump search into every turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {
                        "type": "string",
                        "enum": ["session", "workspace", "user"],
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["auto", "fast", "deep"],
                    },
                    "limit": {"type": "integer"},
                    "before_turn_id": {"type": "string"},
                },
                "required": ["query", "scope"],
                "additionalProperties": False,
            },
            handler=memory_search_tool,
            side_effect_level="read",
            network_access="none",
            idempotent=True,
        )
    )

    async def memory_reflection_tool(
        args: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        if ctx.memory is None or getattr(ctx.memory, "reflection", None) is None:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "reflection memory is not configured")
            )
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            status = str(args.get("status") or "").strip() or None
            return {"candidates": ctx.memory.reflection.list(status=status)}
        if action in {"accept", "reject"}:
            user_text = str(getattr(ctx, "user_text", "") or "").casefold()
            markers = (
                ("接受", "同意", "确认", "设为长期", "记住这个", "accept", "approve", "confirm")
                if action == "accept"
                else ("拒绝", "不同意", "不要设", "reject", "decline")
            )
            if not any(marker in user_text for marker in markers):
                raise AriadneError(
                    app_error(
                        "ARIADNE_TOOL_DENIED",
                        "reflection decisions require explicit confirmation in the current user message",
                    )
                )
            return ctx.memory.reflection.decide(
                candidate_id=str(args.get("candidate_id") or ""),
                action=action,
                user_model=getattr(ctx.memory, "user_model", None),
                workspace_key=str(getattr(ctx.memory, "workspace_key", "") or ""),
                session_id=ctx.session_id,
            )
        raise AriadneError(
            app_error(
                "ARIADNE_INVALID_TOOL_ARGS", "reflection action must be list|accept|reject"
            )
        )

    registry.register(
        ToolSpec(
            name="memory_reflection",
            catalog_description="review cross-session memory suggestions",
            description=(
                "List, accept, or reject evidence-backed cross-session pattern suggestions. "
                "Inferred preferences never become active until accept is explicitly requested."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "accept", "reject"],
                    },
                    "candidate_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "accepted", "rejected"],
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=memory_reflection_tool,
            tool_exposure="named_deferred",
            side_effect_level="unknown",
            side_effect_resolver=lambda args: (
                "read" if str(args.get("action") or "list") == "list" else "write"
            ),
            network_access="none",
            idempotent=None,
        )
    )

    async def prospective_memory_tool(
        args: dict[str, Any], ctx: ToolContext
    ) -> dict[str, Any]:
        if ctx.memory is None or getattr(ctx.memory, "prospective", None) is None:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "prospective memory is not configured")
            )
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            status = str(args.get("status") or "").strip() or None
            return {"entries": ctx.memory.prospective.list(status=status)}
        if action == "create":
            trigger = args.get("trigger")
            if not isinstance(trigger, dict):
                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "trigger must be an object")
                )
            return ctx.memory.prospective.create(
                content=str(args.get("content") or ""),
                trigger=trigger,
                source_session_id=ctx.session_id,
                source_turn_id=ctx.turn_id,
                idempotency_key=str(args.get("idempotency_key") or ""),
            )
        if action in {"cancel", "complete"}:
            return ctx.memory.prospective.transition(
                entry_id=str(args.get("entry_id") or ""), action=action
            )
        raise AriadneError(
            app_error(
                "ARIADNE_INVALID_TOOL_ARGS",
                "prospective action must be create|list|cancel|complete",
            )
        )

    registry.register(
        ToolSpec(
            name="prospective_memory",
            catalog_description="future reminder with structured triggers",
            description=(
                "Create/list/cancel/complete a future reminder. Triggers are an AND of "
                "workspace_equals, path_glob, text_contains, tool_name, event_type, entity_id. "
                "The kernel matches observations; external timers and polling belong to the host."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "cancel", "complete"],
                    },
                    "entry_id": {"type": "string"},
                    "content": {"type": "string"},
                    "trigger": {
                        "type": "object",
                        "properties": {
                            key: {
                                "oneOf": [
                                    {"type": "string"},
                                    {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                    },
                                ]
                            }
                            for key in (
                                "workspace_equals",
                                "path_glob",
                                "text_contains",
                                "tool_name",
                                "event_type",
                                "entity_id",
                            )
                        },
                        "additionalProperties": False,
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "triggered", "completed", "cancelled"],
                    },
                    "idempotency_key": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=prospective_memory_tool,
            tool_exposure="named_deferred",
            side_effect_level="unknown",
            side_effect_resolver=lambda args: (
                "read" if str(args.get("action") or "list") == "list" else "write"
            ),
            network_access="none",
            idempotent=None,
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
                "set_attribute {entity_id, key, value, memory_type?, authority?}; "
                "expire_attribute {entity_id, key, authority?}; "
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
                                        "expire_attribute",
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
                                "memory_type": {
                                    "type": "string",
                                    "enum": ["fact", "preference", "goal", "hypothesis"],
                                },
                                "authority": {
                                    "type": "string",
                                    "enum": ["model_inferred", "tool_observed", "user_explicit"],
                                },
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
            side_effect_level="unknown",
            side_effect_resolver=lambda args: (
                "read" if str(args.get("action") or "read") == "read" else "write"
            ),
            network_access="none",
            idempotent=None,
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
            side_effect_level="read",
            network_access="none",
            idempotent=True,
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
        section = str(args.get("section") or "").strip() or None
        targeted: list[str] | None = None
        if isinstance(ref_names, list) and ref_names:
            targeted = [str(x) for x in ref_names]
            include_refs = True
        # requires_tools enforcement: report missing tools (do not invent handlers).
        available = set(registry.tools.keys())
        missing = ctx.skills.missing_tools(skill, available)
        body_text = skill.body_section(section) if section else skill.body
        import hashlib

        content_digest = hashlib.sha256(
            (body_text or "").encode("utf-8")
        ).hexdigest()[:16]
        if ctx.skill_events is not None:
            from ..types import SkillEvent

            ctx.skill_events.append(
                SkillEvent(
                    kind="load",
                    skill_name=name,
                    content_digest=content_digest,
                )
            )
        payload: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description,
            "body": body_text,
            "content_digest": content_digest,
            "section": section or "full",
            "requires_tools": skill.requires_tools,
            "missing_tools": missing,
            "requires_tools_ok": not missing,
            "namespace": skill.namespace,
            "version": skill.version,
            "tags": skill.tags,
            "distinct_from": skill.distinct_from,
            "trigger_clues": skill.trigger_clues,
            "key_difference": skill.key_difference,
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
                "Optional section=usage|schema|examples|… loads only that ## heading. "
                "Optional references=[name.md,…] loads only those files; "
                "include_references=true loads all. Reports missing requires_tools."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "section": {
                        "type": "string",
                        "description": "Markdown ## section name, or full",
                    },
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
            side_effect_level="read",
            network_access="none",
            idempotent=True,
        )
    )

    async def adopt_skill(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.skills is None or ctx.skill_events is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "skill trace is unavailable"))
        name = str(args.get("name") or "").strip()
        if ctx.skills.get(name) is None:
            raise AriadneError(
                app_error("ARIADNE_SKILL_NOT_FOUND", f"skill not found: {name}", name=name)
            )
        loaded = any(
            event.kind == "load" and event.skill_name == name
            and "skipped" not in event.detail
            for event in ctx.skill_events
        )
        if not loaded:
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_ADOPTION_INVALID",
                    "load the skill before explicitly adopting its guidance",
                    name=name,
                )
            )
        from ..types import SkillEvent

        if not any(
            event.kind == "adopt" and event.skill_name == name
            for event in ctx.skill_events
        ):
            ctx.skill_events.append(
                SkillEvent(
                    kind="adopt",
                    skill_name=name,
                    detail=str(args.get("reason") or "explicit model adoption").strip(),
                )
            )
        return {"name": name, "adopted": True}

    registry.register(
        ToolSpec(
            name="adopt_skill",
            catalog_description="declare use of loaded skill guidance",
            description=(
                "Explicitly declare that this turn is following a previously loaded skill. "
                "Loading alone is not adoption and receives no success credit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
            handler=adopt_skill,
            title="Adopt skill",
            kind="tool",
            side_effect_level="none",
            network_access="none",
            idempotent=True,
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
        if action == "propose_update":
            evidence = args.get("evidence") or []
            if not isinstance(evidence, list):
                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "evidence must be a list")
                )
            return ctx.skills.patches().propose(
                name=name,
                description=str(args.get("description") or ""),
                body=str(args.get("body") or ""),
                keywords=[str(x) for x in keywords],
                evidence=[str(x) for x in evidence],
                expected_version=str(args.get("expected_version") or ""),
            )
        if action == "list_proposals":
            return {
                "proposals": ctx.skills.patches().list(
                    status=str(args.get("status") or "") or None
                )
            }
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
            description=(
                "Create/delete user skills, or propose an evidence-backed update. "
                "Updates return a diff and remain pending until the host user confirms them; "
                "the model cannot confirm its own proposal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "delete", "propose_update", "list_proposals"],
                    },
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "body": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "expected_version": {"type": "string"},
                    "status": {"type": "string", "enum": ["pending", "applied", "rejected"]},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            handler=skill_manage,
            tool_exposure="named_deferred",
            title="Skill manage",
            side_effect_level="write",
            side_effect_resolver=lambda args: (
                "destructive"
                if str(args.get("action") or "") == "delete"
                else (
                    "read"
                    if str(args.get("action") or "") == "list_proposals"
                    else "write"
                )
            ),
            network_access="none",
            idempotent=False,
            verification_hint=("path_exists",),
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
            side_effect_level="none",
            network_access="none",
            idempotent=True,
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
                side_effect_level="none",
                network_access="none",
                idempotent=True,
            )
        )

    from .filetools import register_file_tools

    register_file_tools(registry)
    registry.ensure_titles()
    return registry
