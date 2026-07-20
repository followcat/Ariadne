from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..errors import AriadneError, app_error
from ..memory.facade import MemoryFacade
from ..model.base import ModelPort
from ..guardrails import scan_input, scan_output
from ..redact import redact_secrets
from ..sandbox.active import ActiveSessionManager
from ..sandbox.port import SandboxBackend, SandboxSession
from ..skills.store import SkillStore
from ..tools.registry import ApprovalHook, ToolContext, ToolRegistry, dumps_tool_output
from ..types import (
    Message,
    RunTurnCommand,
    SchemaMetrics,
    SkillEvent,
    ToolCallTrace,
    TurnEvent,
    TurnResult,
    Usage,
)


SYSTEM_POLICY = """You are Ariadne, a local shell agent working inside a user project directory.

Filesystem contract for sandbox_exec:
- Default cwd is the project root (logical name: /workspace). Prefer relative paths.
- Scratch directory is logical /session (cwd="/session"); also available as $ARIADNE_SESSION_DIR.
- Shell variables (cd/export) do NOT persist across sandbox_exec calls.

Skills:
- Skills teach procedures; tools act. Skills do not replace tools.
- Prefer skills listed under recommended / auto_load when they match the user goal.
- If none match, call search_skills with the user intent before inventing multi-step domain workflows.
- Call load_skill before relying on a skill body; do not invent domain runbooks from memory alone.
- Do not paste entire skill references unless needed; load targeted content via load_skill.
- skill_manage can create/update user skills (not builtin packs).

Memory:
- conversation_state is authoritative for current-session facts/todos.
- memory tool is for durable preferences across sessions.
- Semantic hits and summaries are historical and may be superseded by conversation_state.

Rules:
1. Use tools when needed; prefer sandbox_exec for computer work.
2. Prefer non-interactive commands.
3. After tools finish, give a concise final answer.
4. Never invent tool results.
5. If a command fails, recover or explain.
6. Cross-tool: do not batch durable memory writes with unrelated sandbox side effects in one step.
"""


EventSink = Callable[[TurnEvent], Awaitable[None] | None]


class _SandboxGuard:
    """Closes the turn's sandbox session on every exit path.

    Covers: normal completion, turn failure, memory-build errors before the
    tool loop, and host cancellation (GeneratorExit) — no leaked /session
    dirs or docker containers.
    """

    def __init__(self, app: "TurnApplication", *, session_id: str) -> None:
        self._app = app
        self._session_id = session_id
        self._sandbox: SandboxSession | None = None
        self._task: asyncio.Task[SandboxSession] | None = None
        self._released = False

    def attach(self, sandbox: SandboxSession) -> None:
        self._sandbox = sandbox

    def attach_task(self, task: "asyncio.Task[SandboxSession]") -> None:
        self._task = task

    async def release(self) -> None:
        if self._released or self._sandbox is None:
            return
        self._released = True
        await self._app._release_sandbox(session_id=self._session_id, sandbox=self._sandbox)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                sandbox = await self._task
            except BaseException:
                sandbox = None
            self._task = None
            if sandbox is not None:
                self._sandbox = sandbox
        await self.release()


def _public_messages(messages: list[dict[str, Any]]) -> list[Message]:
    """Turn conversation for hosts: excludes internal system assembly."""
    out: list[Message] = []
    for m in messages:
        role = str(m.get("role") or "")
        if role == "system":
            continue
        out.append(
            Message(
                role=role,  # type: ignore[arg-type]
                content=str(m.get("content") or ""),
                tool_call_id=m.get("tool_call_id"),
                name=m.get("name"),
                tool_calls=m.get("tool_calls"),
            )
        )
    return out


@dataclass
class TurnApplication:
    model: ModelPort
    tools: ToolRegistry
    memory: MemoryFacade
    skills: SkillStore
    sandbox_backend: SandboxBackend | None = None
    active_sessions: ActiveSessionManager | None = None
    tool_loop_limit: int = 32
    prefer_deferred_tools: bool = True
    # function | native | none — see ToolRegistry.build_exposure client_search_mode
    tool_search_mode: str = "function"
    sandbox_mode: str = "per_turn"  # per_turn | active_session
    stream_model: bool = False
    sandbox_prestart: bool = False
    sandbox_prestart_limit: int = 4
    redact_traces: bool = True
    guardrails_enabled: bool = True
    approval_hook: ApprovalHook | None = None
    vision_mode: str = "auto"  # auto | on | off — see multimodal.model_supports_vision
    # Optional per-session tool allow-list (None = all exposed tools).
    session_visible_tools: set[str] | None = None
    _sandbox_start_semaphore: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def _start_semaphore(self) -> asyncio.Semaphore:
        if self._sandbox_start_semaphore is None:
            self._sandbox_start_semaphore = asyncio.Semaphore(self.sandbox_prestart_limit)
        return self._sandbox_start_semaphore

    async def run(
        self,
        *,
        prompt: str,
        session_id: str,
        model: str | None = None,
        user_id: str | None = None,
        tool_loop_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
        on_event: EventSink | None = None,
        images: list[Any] | None = None,
    ) -> TurnResult:
        events: AsyncIterator[TurnEvent] = self.run_events(
            prompt=prompt,
            session_id=session_id,
            model=model,
            user_id=user_id,
            tool_loop_limit=tool_loop_limit,
            metadata=metadata,
            images=images,
        )
        final: TurnResult | None = None
        async for event in events:
            if on_event is not None:
                maybe = on_event(event)
                if maybe is not None:
                    await maybe
            if event.kind in {"turn_completed", "turn_failed"}:
                final = event.data.get("result")
        assert final is not None
        return final

    async def run_command(self, command: RunTurnCommand) -> TurnResult:
        prompt = command.input if isinstance(command.input, str) else "\n".join(
            m.content for m in command.input if m.role == "user"
        )
        return await self.run(
            prompt=prompt,
            session_id=command.session_id,
            model=command.model,
            user_id=command.user_id,
            tool_loop_limit=command.tool_loop_limit,
            metadata=command.metadata,
        )

    async def run_events(
        self,
        *,
        prompt: str,
        session_id: str,
        model: str | None = None,
        user_id: str | None = None,
        tool_loop_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
        images: list[Any] | None = None,
    ) -> AsyncIterator[TurnEvent]:
        guard = _SandboxGuard(self, session_id=session_id)
        try:
            async for event in self._run_events_inner(
                prompt=prompt,
                session_id=session_id,
                model=model,
                user_id=user_id,
                tool_loop_limit=tool_loop_limit,
                metadata=metadata,
                images=images,
                guard=guard,
            ):
                yield event
        finally:
            await guard.close()

    async def _run_events_inner(
        self,
        *,
        prompt: str,
        session_id: str,
        model: str | None = None,
        user_id: str | None = None,
        tool_loop_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
        images: list[Any] | None = None,
        guard: _SandboxGuard,
    ) -> AsyncIterator[TurnEvent]:
        from ..multimodal import (
            ImageAttachment,
            build_user_message_content,
            ensure_vision_allowed,
            transcript_user_line,
        )

        image_list: list[ImageAttachment] = list(images or [])
        model_name = model or getattr(self.model, "model", None) or "unknown"
        if image_list:
            ensure_vision_allowed(str(model_name), image_list, vision_mode=self.vision_mode)

        turn_id = uuid.uuid4().hex[:12]
        loop_limit = tool_loop_limit if tool_loop_limit is not None else self.tool_loop_limit
        started_at = datetime.now(timezone.utc)
        yield TurnEvent(
            "turn_started",
            {
                "turn_id": turn_id,
                "session_id": session_id,
                "metadata": dict(metadata or {}),
                "image_count": len(image_list),
            },
        )

        if self.guardrails_enabled:
            prompt, in_findings = scan_input(prompt)
            for finding in in_findings:
                yield TurnEvent(
                    "guard_finding",
                    {"direction": "in", "kind": finding.kind, "detail": finding.detail},
                )

        sandbox_task: asyncio.Task[SandboxSession] | None = None
        if self.sandbox_prestart:
            # prestart in parallel with memory build (bounded so parallel
            # agents cannot fork-bomb the host)
            async def _prestart() -> SandboxSession:
                async with self._start_semaphore():
                    return await self._acquire_sandbox(session_id=session_id, turn_id=turn_id)

            sandbox_task = asyncio.create_task(_prestart())
            guard.attach_task(sandbox_task)
            sandbox: SandboxSession | None = None
        else:
            sandbox = await self._acquire_sandbox(session_id=session_id, turn_id=turn_id)
            guard.attach(sandbox)
        tool_calls: list[ToolCallTrace] = []
        skill_events: list[SkillEvent] = []
        usage_total = Usage()
        schema_metrics: list[SchemaMetrics] = []

        memory_system, memory_summary = await self.memory.build_context_async(
            session_id=session_id, user_id=user_id, query=prompt
        )
        if sandbox_task is not None:
            sandbox = await sandbox_task
            guard.attach(sandbox)
        for layer in memory_summary.layers:
            yield TurnEvent(
                "memory_layer",
                {"name": layer.name, "status": layer.status, "token_chars": layer.token_chars},
            )

        # Hybrid plan when available (falls back to lexical).
        if hasattr(self.skills, "plan_async"):
            skill_plan = await self.skills.plan_async(prompt)
        else:
            skill_plan = self.skills.plan(prompt)
        exposure = self.tools.build_exposure(
            prefer_deferred=self.prefer_deferred_tools,
            session_visible=self.session_visible_tools,
            client_search_mode=self.tool_search_mode,
        )
        catalog = self.tools.catalog_text(session_visible=self.session_visible_tools)
        if exposure.client_search_mode == "native" and exposure.deferred_tools:
            # Provider-native path: deferred names stay discoverable in catalog;
            # first invoke auto-materializes (no tool_search function on wire).
            deferred_names = ", ".join(sorted(exposure.deferred_tools.keys()))
            catalog = (
                (catalog + "\n" if catalog else "")
                + f"(native deferred load: {deferred_names})"
            )

        # Prompt assembly (design: policy → high-signal memory → skills → catalog
        # → runtime → recent → user). Skill plan sits near user (strong attention).
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_POLICY},
        ]
        if memory_system:
            messages.append({"role": "system", "content": memory_system})

        # Always emit compact SKILL_SELECTION (never dump full linear index).
        n_skills = len(self.skills.list())
        if hasattr(self.skills, "format_plan_text"):
            plan_text = self.skills.format_plan_text(skill_plan, n_skills=n_skills)
        else:
            plan_text = "[SKILL_SELECTION]\nother: use search_skills"
        report = skill_plan.get("report") or {}
        detail = (
            f"plan: {len(skill_plan['auto_load'])} auto, "
            f"{len(skill_plan['recommended'])} recommended, "
            f"{skill_plan.get('other', 0)} other; "
            f"plan_chars={report.get('plan_chars', len(plan_text))}"
        )
        messages.append({"role": "system", "content": plan_text})
        skill_events.append(SkillEvent(kind="index", detail=detail))
        yield TurnEvent("skill_event", {"kind": "index", "detail": detail})

        # Turn-scoped auto_load bodies (SKILLS §4.1 / §5) — not permanent policy.
        budgets = getattr(self.skills, "budgets", None)
        body_max = int(getattr(budgets, "auto_body_max", 2) or 2)
        body_chars = int(getattr(budgets, "auto_body_chars", 6000) or 6000)
        available_tools = set(self.tools.tools.keys())
        for skill, score in skill_plan["auto_load"][:body_max]:
            body = (skill.body or "").strip()
            if not body:
                continue
            missing = []
            if hasattr(self.skills, "missing_tools"):
                missing = self.skills.missing_tools(skill, available_tools)
            if missing:
                # Enforce requires_tools: inject note instead of body.
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            f"[SKILL_BODY name={skill.name} score={score:.3g} "
                            f"scope=this_turn skipped=missing_tools]\n"
                            f"requires_tools missing: {', '.join(missing)}. "
                            "Register/enable those tools before following this skill."
                        ),
                    }
                )
                skill_events.append(
                    SkillEvent(
                        kind="load",
                        skill_name=skill.name,
                        detail=f"auto_load skipped missing_tools={missing}",
                    )
                )
                yield TurnEvent(
                    "skill_event",
                    {
                        "kind": "load",
                        "skill_name": skill.name,
                        "detail": f"auto_load skipped missing_tools={missing}",
                    },
                )
                continue
            if len(body) > body_chars:
                body = body[:body_chars] + f"\n[ariadne: skill body truncated to {body_chars} chars]"
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"[SKILL_BODY name={skill.name} score={score:.3g} scope=this_turn]\n"
                        f"{body}"
                    ),
                }
            )
            skill_events.append(
                SkillEvent(kind="load", skill_name=skill.name, detail=f"auto_load score={score:.3g}")
            )
            yield TurnEvent(
                "skill_event",
                {"kind": "load", "skill_name": skill.name, "detail": f"auto_load score={score:.3g}"},
            )

        # Short tool catalog (discovery); full schemas go on the wire separately.
        messages.append(
            {
                "role": "system",
                "content": "Available tools (catalog):\n" + (catalog or "(none)"),
            }
        )

        messages.append(
            {
                "role": "system",
                "content": (
                    "[RUNTIME_CONTEXT]\n"
                    f"now_utc: {started_at.isoformat()}\n"
                    f"session_id: {session_id}\n"
                    f"turn_id: {turn_id}"
                ),
            }
        )

        for prior in self.memory.recent_messages(session_id=session_id):
            messages.append(prior)

        user_content = build_user_message_content(prompt, image_list or None)
        user_transcript = transcript_user_line(prompt, image_list or None)
        messages.append({"role": "user", "content": user_content})
        self.memory.transcript.append(
            {
                "role": "user",
                "content": user_transcript,
                "turn_id": turn_id,
                "session_id": session_id,
            }
        )

        evidence_parts = [user_transcript]
        ctx = ToolContext(
            session_id=session_id,
            turn_id=turn_id,
            sandbox=sandbox,
            memory=self.memory,
            skills=self.skills,
            exposure=exposure,
            skill_events=skill_events,
            evidence_text=user_transcript,
            approval_hook=self.approval_hook,
        )

        try:
            for loop_i in range(loop_limit):
                tools_payload = exposure.request_tools or None
                schema_metrics.append(
                    SchemaMetrics(
                        exchange_index=loop_i,
                        tool_count=len(tools_payload or []),
                        schema_chars=self.tools.schema_chars_for(tools_payload or []),
                        catalog_chars=len(catalog),
                        deferred_count=len(exposure.deferred_tools),
                        loaded_deferred=sorted(exposure.loaded_tool_names),
                    )
                )

                if self.stream_model and hasattr(self.model, "stream"):
                    exchange = None
                    async for sev in self.model.stream(
                        messages=messages, tools=tools_payload, model=model
                    ):
                        if sev.kind == "delta" and sev.text:
                            yield TurnEvent("model_delta", {"text": sev.text})
                        if sev.kind == "completed" and sev.exchange is not None:
                            exchange = sev.exchange
                    if exchange is None:
                        exchange = await self.model.complete(
                            messages=messages, tools=tools_payload, model=model
                        )
                else:
                    exchange = await self.model.complete(
                        messages=messages, tools=tools_payload, model=model
                    )

                usage_total.prompt_tokens += exchange.usage.prompt_tokens
                usage_total.completion_tokens += exchange.usage.completion_tokens
                usage_total.total_tokens += exchange.usage.total_tokens
                usage_total.reasoning_tokens += exchange.usage.reasoning_tokens

                assistant = exchange.message
                tool_calls_payload = assistant.tool_calls or []
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": assistant.content or None,
                }
                if tool_calls_payload:
                    assistant_msg["tool_calls"] = tool_calls_payload
                messages.append(assistant_msg)
                if assistant.content:
                    evidence_parts.append(assistant.content)
                    ctx.evidence_text = "\n".join(evidence_parts)

                if not tool_calls_payload:
                    text = (assistant.content or "").strip()
                    if self.guardrails_enabled:
                        text, out_findings = scan_output(text)
                        for finding in out_findings:
                            yield TurnEvent(
                                "guard_finding",
                                {"direction": "out", "kind": finding.kind, "detail": finding.detail},
                            )
                    self.memory.transcript.append(
                        {
                            "role": "assistant",
                            "content": text,
                            "turn_id": turn_id,
                            "session_id": session_id,
                        }
                    )
                    # L1: enqueue source then process to ready (status machine).
                    source_for_summary = text if text else f"user asked: {prompt[:200]}"
                    self.memory.summaries.enqueue(
                        session_id=session_id,
                        turn_id=turn_id,
                        source_text=source_for_summary,
                    )
                    self.memory.summaries.process_pending(session_id=session_id, max_jobs=4)
                    summary = source_for_summary[:400]
                    tool_blob = "\n".join(
                        f"{c.name}: {json.dumps(c.output, ensure_ascii=False)[:300]}"
                        for c in tool_calls
                        if c.status == "completed"
                    )
                    # Tag only entities mentioned this turn (not the entire state set).
                    evidence_blob = "\n".join(
                        [prompt, text, tool_blob]
                    )
                    entity_ids = self.memory.entities_mentioned_in_text(
                        session_id, evidence_blob
                    )
                    self.memory.semantic.index_turn(
                        session_id=session_id,
                        turn_id=turn_id,
                        user_text=prompt,
                        assistant_text=text,
                        tool_text=tool_blob,
                        summary_text=summary,
                        entity_ids=entity_ids,
                    )
                    # enqueue projection job for optional worker
                    if getattr(self.memory, "projection", None) is not None:
                        self.memory.projection.enqueue(
                            session_id=session_id,
                            turn_id=turn_id,
                            evidence_text="\n".join(evidence_parts)[:8000],
                        )
                    await guard.release()
                    result = TurnResult(
                        turn_id=turn_id,
                        status="completed",
                        text=text,
                        messages=_public_messages(messages),
                        tool_calls=tool_calls,
                        skill_events=skill_events,
                        memory=memory_summary,
                        usage=usage_total,
                        session_id=session_id,
                        model=model or self.model.model,
                        schema_metrics=schema_metrics,
                    )
                    yield TurnEvent("turn_completed", {"result": result})
                    return

                for call in tool_calls_payload:
                    call_id = str(call.get("id") or uuid.uuid4().hex)
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments") or "{}"
                    started = datetime.now(timezone.utc)
                    yield TurnEvent("tool_started", {"call_id": call_id, "name": name})
                    args: dict[str, Any] = {}
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        if not isinstance(args, dict):
                            raise AriadneError(
                                app_error(
                                    "ARIADNE_INVALID_TOOL_ARGS",
                                    "tool arguments must be a JSON object",
                                )
                            )
                        output = await self.tools.invoke(name, args, ctx)
                        trace_args = redact_secrets(args) if self.redact_traces else args
                        if self.redact_traces:
                            output = redact_secrets(output)
                        finished = datetime.now(timezone.utc)
                        spec = self.tools.get(name)
                        trace = ToolCallTrace(
                            call_id=call_id,
                            name=name,
                            arguments=trace_args if isinstance(trace_args, dict) else args,
                            output=output,
                            status="completed",
                            started_at=started,
                            finished_at=finished,
                            schema_chars=spec.schema_chars() if spec else 0,
                        )
                        tool_calls.append(trace)
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": dumps_tool_output(output),
                            }
                        )
                        evidence_parts.append(dumps_tool_output(output)[:2000])
                        ctx.evidence_text = "\n".join(evidence_parts)
                        yield TurnEvent(
                            "tool_completed",
                            {
                                "call_id": call_id,
                                "name": name,
                                "status": "completed",
                                "arguments": trace_args if isinstance(trace_args, dict) else args,
                                "output": output,
                            },
                        )
                        if name in {"search_skills", "load_skill"} and skill_events:
                            yield TurnEvent(
                                "skill_event",
                                {
                                    "kind": skill_events[-1].kind,
                                    "skill_name": skill_events[-1].skill_name,
                                    "detail": skill_events[-1].detail,
                                },
                            )
                    except AriadneError as exc:
                        finished = datetime.now(timezone.utc)
                        err = exc.error
                        fail_args = redact_secrets(args) if self.redact_traces else args
                        tool_calls.append(
                            ToolCallTrace(
                                call_id=call_id,
                                name=name,
                                arguments=fail_args if isinstance(fail_args, dict) else args,
                                output=None,
                                status="failed",
                                error=err,
                                started_at=started,
                                finished_at=finished,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": dumps_tool_output(
                                    {
                                        "error": {
                                            "code": err.code,
                                            "message": err.message,
                                            "details": err.details,
                                        }
                                    }
                                ),
                            }
                        )
                        yield TurnEvent(
                            "tool_completed",
                            {
                                "call_id": call_id,
                                "name": name,
                                "status": "failed",
                                "arguments": fail_args if isinstance(fail_args, dict) else args,
                                "error": {"code": err.code, "message": err.message},
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        finished = datetime.now(timezone.utc)
                        err = app_error("ARIADNE_TOOL_HANDLER_ERROR", f"{type(exc).__name__}: {exc}")
                        tool_calls.append(
                            ToolCallTrace(
                                call_id=call_id,
                                name=name,
                                arguments=args,
                                output=None,
                                status="failed",
                                error=err,
                                started_at=started,
                                finished_at=finished,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": dumps_tool_output(
                                    {"error": {"code": err.code, "message": err.message}}
                                ),
                            }
                        )
                        yield TurnEvent(
                            "tool_completed",
                            {
                                "call_id": call_id,
                                "name": name,
                                "status": "failed",
                                "arguments": args,
                                "error": {"code": err.code, "message": err.message},
                            },
                        )

            await guard.release()
            err = app_error(
                "ARIADNE_TOOL_LOOP_LIMIT",
                f"Exceeded tool loop limit ({loop_limit})",
            )
            result = TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                messages=_public_messages(messages),
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=err,
                session_id=session_id,
                model=model or self.model.model,
                schema_metrics=schema_metrics,
            )
            yield TurnEvent("turn_failed", {"result": result})
        except AriadneError as exc:
            await guard.release()
            result = TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                messages=_public_messages(messages),
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=exc.error,
                session_id=session_id,
                model=model or self.model.model,
                schema_metrics=schema_metrics,
            )
            yield TurnEvent("turn_failed", {"result": result})
        except Exception as exc:  # noqa: BLE001
            await guard.release()
            result = TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                messages=_public_messages(messages),
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=app_error("ARIADNE_MODEL_ERROR", f"{type(exc).__name__}: {exc}"),
                session_id=session_id,
                model=model or self.model.model,
                schema_metrics=schema_metrics,
            )
            yield TurnEvent("turn_failed", {"result": result})

    async def _acquire_sandbox(self, *, session_id: str, turn_id: str) -> SandboxSession:
        if self.sandbox_mode == "active_session" and self.active_sessions is not None:
            return await self.active_sessions.get_or_start(session_id=session_id)
        if self.sandbox_backend is None:
            raise AriadneError(app_error("ARIADNE_SANDBOX_DISABLED", "No sandbox backend configured"))
        return await self.sandbox_backend.start(scope_key=f"{session_id}-{turn_id}")

    async def _release_sandbox(self, *, session_id: str, sandbox: SandboxSession) -> None:
        if self.sandbox_mode == "active_session" and self.active_sessions is not None:
            await self.active_sessions.release_turn(session_id=session_id, keep_alive=True)
            return
        await sandbox.close(reason="turn_finished")
