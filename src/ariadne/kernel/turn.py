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
from ..redact import redact_secrets
from ..sandbox.active import ActiveSessionManager
from ..sandbox.port import SandboxBackend, SandboxSession
from ..skills.store import SkillStore
from ..tools.registry import ToolContext, ToolRegistry, dumps_tool_output
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
- A short skill index may be provided. Use search_skills / load_skill when needed.
- skill_manage can create/update user skills.
- Skills teach; tools act.

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
    sandbox_mode: str = "per_turn"  # per_turn | active_session
    stream_model: bool = False
    sandbox_prestart: bool = False
    sandbox_prestart_limit: int = 4
    redact_traces: bool = True
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
    ) -> TurnResult:
        events: AsyncIterator[TurnEvent] = self.run_events(
            prompt=prompt,
            session_id=session_id,
            model=model,
            user_id=user_id,
            tool_loop_limit=tool_loop_limit,
            metadata=metadata,
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
        guard: _SandboxGuard,
    ) -> AsyncIterator[TurnEvent]:
        turn_id = uuid.uuid4().hex[:12]
        loop_limit = tool_loop_limit if tool_loop_limit is not None else self.tool_loop_limit
        started_at = datetime.now(timezone.utc)
        yield TurnEvent(
            "turn_started",
            {"turn_id": turn_id, "session_id": session_id, "metadata": dict(metadata or {})},
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

        skill_plan = self.skills.plan(prompt)
        exposure = self.tools.build_exposure(prefer_deferred=self.prefer_deferred_tools)
        catalog = self.tools.catalog_text()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_POLICY},
            {"role": "system", "content": "Available tools (catalog):\n" + (catalog or "(none)")},
        ]
        if memory_system:
            messages.append({"role": "system", "content": memory_system})

        # skill selection plan sits in a strong attention region near user input
        if skill_plan["auto_load"] or skill_plan["recommended"]:
            lines = ["[SKILL_SELECTION]"]
            if skill_plan["auto_load"]:
                names = ", ".join(s.name for s, _ in skill_plan["auto_load"])
                lines.append(f"auto_load: {names} (call load_skill now)")
            if skill_plan["recommended"]:
                names = ", ".join(
                    f"{s.name} (score {score})" for s, score in skill_plan["recommended"]
                )
                lines.append(f"recommended: {names}")
            if skill_plan["other"]:
                lines.append(f"other: {skill_plan['other']} more installed — use search_skills")
            plan_text = "\n".join(lines)
            detail = (
                f"plan: {len(skill_plan['auto_load'])} auto, "
                f"{len(skill_plan['recommended'])} recommended, {skill_plan['other']} other"
            )
            messages.append({"role": "system", "content": plan_text})
            skill_events.append(SkillEvent(kind="index", detail=detail))
            yield TurnEvent("skill_event", {"kind": "index", "detail": detail})
        else:
            skill_index = self.skills.index_text()
            if skill_index and skill_index != "(no skills installed)":
                messages.append({"role": "system", "content": "Skill index:\n" + skill_index})
                skill_events.append(
                    SkillEvent(kind="index", detail=f"{len(self.skills.list())} skills")
                )
                yield TurnEvent("skill_event", {"kind": "index", "detail": skill_events[-1].detail})

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

        for prior in self.memory.recent_messages():
            messages.append(prior)

        messages.append({"role": "user", "content": prompt})
        self.memory.transcript.append({"role": "user", "content": prompt, "turn_id": turn_id})

        evidence_parts = [prompt]
        ctx = ToolContext(
            session_id=session_id,
            turn_id=turn_id,
            sandbox=sandbox,
            memory=self.memory,
            skills=self.skills,
            exposure=exposure,
            skill_events=skill_events,
            evidence_text=prompt,
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
                    self.memory.transcript.append(
                        {"role": "assistant", "content": text, "turn_id": turn_id}
                    )
                    summary = text[:400] if text else f"user asked: {prompt[:200]}"
                    self.memory.summaries.put(
                        session_id=session_id, turn_id=turn_id, summary_text=summary
                    )
                    tool_blob = "\n".join(
                        f"{c.name}: {json.dumps(c.output, ensure_ascii=False)[:300]}"
                        for c in tool_calls
                        if c.status == "completed"
                    )
                    state = self.memory.state.get(session_id)
                    entity_ids = list((state.get("entities") or {}).keys())
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
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("tool arguments must be a JSON object")
                        output = await self.tools.invoke(name, args, ctx)
                        if self.redact_traces:
                            output = redact_secrets(output)
                        finished = datetime.now(timezone.utc)
                        spec = self.tools.get(name)
                        trace = ToolCallTrace(
                            call_id=call_id,
                            name=name,
                            arguments=args,
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
                            {"call_id": call_id, "name": name, "status": "completed", "output": output},
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
                        tool_calls.append(
                            ToolCallTrace(
                                call_id=call_id,
                                name=name,
                                arguments={},
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
                                arguments={},
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
