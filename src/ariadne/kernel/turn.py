from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..context import ContextAttribution, ContextBlock, ContextCompiler
from ..errors import AriadneError, app_error
from ..memory.facade import MemoryFacade
from ..memory.projection import ProjectorFn
from ..model.base import ModelPort
from ..redact import redact_secrets
from ..guardrails import scan_input, scan_output
from ..sandbox.active import ActiveSessionManager
from ..sandbox.port import SandboxBackend, SandboxSession
from ..skills.store import SkillStore
from ..tasks.controller import (
    REVISE_TASK_PLAN_NAME,
    REVISE_TASK_PLAN_TOOL,
    SUBMIT_TASK_PLAN_NAME,
    SUBMIT_TASK_PLAN_TOOL,
    TaskController,
)
from ..tasks.models import TaskState, TaskSummary
from ..tasks.policy import resolve_task_mode
from ..tasks.protocol import payload_has_tool
from ..tasks.runtime import (
    apply_revise_task_plan,
    apply_submit_task_plan,
    bootstrap_task_session,
    finalize_attempt,
    prepare_capability_exchange,
    resolve_final_answer_status,
    select_task_tools_payload,
)
from ..tools.registry import ApprovalHook, ToolContext, ToolRegistry
from ..types import (
    Message,
    LayerReport,
    RunTurnCommand,
    SchemaMetrics,
    SkillEvent,
    ToolCallTrace,
    TurnEvent,
    TurnResult,
    Usage,
)
from .tool_exchange import invoke_tool_exchange


SYSTEM_POLICY = """You are Ariadne, a local agent working inside a user project directory.

Filesystem contract:
- Durable project root: /workspace. Scratch: /session.
- PREFERRED tools: sandbox_read_file, sandbox_write_file, sandbox_edit_file, sandbox_list_dir, sandbox_delete_file.
- FALLBACK: sandbox_exec for shell only when file tools are insufficient. Shell state does NOT persist.
- HTTP: use web_fetch on the host (container has no network by default). Do not curl inside the sandbox unless egress is explicitly enabled.

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
1. Prefer semantic file tools over sandbox_exec for file work.
2. Prefer non-interactive commands when shell is required.
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


_EMPTY_RECOVERY_REASONING_LIMIT = 1200


def _recover_empty_assistant_text(
    *,
    reasoning: str,
    tool_calls: list[Any],
) -> str:
    """When the model ends a turn with empty content, surface something usable.

    Prefer truncated reasoning (honest, not invented business conclusions).
    If tools ran, append a short delivery nudge so workshop tasks can continue.
    """
    reason = (reasoning or "").strip()
    completed = [
        c
        for c in tool_calls
        if getattr(c, "status", None) == "completed" or getattr(c, "name", None)
    ]
    tool_names = []
    for c in completed:
        name = getattr(c, "name", None) or ""
        if name and name not in tool_names:
            tool_names.append(name)

    parts: list[str] = []
    if reason:
        clipped = reason
        if len(clipped) > _EMPTY_RECOVERY_REASONING_LIMIT:
            clipped = clipped[: _EMPTY_RECOVERY_REASONING_LIMIT - 20].rstrip() + "…"
        parts.append("（刚才光在脑子里想、没说出口，摘一点给你看）\n\n" + clipped)

    if tool_names:
        names = "、".join(tool_names[:12])
        parts.append(
            "这轮我翻过这些："
            + names
            + "。\n"
            "但还没真正改好文件、也没跟你说清楚结果。\n"
            "你可以直接再说一句，比如：「接着改，把小鸟画进去并保存文件」。"
        )
    elif not reason:
        parts.append(
            "这轮好像卡住了，没给出正文。\n"
            "再试一次就好；可以说得随便一点，比如「帮我改改」「接着画」。"
        )

    return "\n\n".join(parts).strip()


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
    # In-process RuntimeAgent (policy + egress); bound to sandbox each turn.
    runtime_agent: Any | None = None
    # Host-injected system block (Atelier KNOWLEDGE, etc.)
    extra_system_prompt: str = ""
    # Completion budget per model call (default 8k; atelier often 16k).
    max_tokens: int = 8192
    task_controller: TaskController | None = None
    # off | on | auto — see tasks.policy.resolve_task_mode
    task_mode_policy: str = "auto"
    context_compiler: ContextCompiler = field(default_factory=ContextCompiler)
    memory_projector: ProjectorFn | None = None
    available_credentials: frozenset[str] = frozenset()
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

        task_state: TaskState | None = None
        active_probe = None
        if self.task_controller is not None:
            active_probe = self.task_controller.load_active(session_id)
        task_mode, task_mode_reason = resolve_task_mode(
            policy=self.task_mode_policy,
            metadata=metadata,
            has_active_task=active_probe is not None,
        )
        yield TurnEvent(
            "task_mode_resolved",
            {
                "enabled": task_mode,
                "reason": task_mode_reason,
                "policy": self.task_mode_policy,
            },
        )
        if task_mode:
            if self.task_controller is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_TASK_UNAVAILABLE",
                        "task mode requires a configured TaskController",
                    )
                )
            boot = bootstrap_task_session(
                controller=self.task_controller,
                active=active_probe,
                prompt=prompt,
                task_mode_reason=task_mode_reason,
            )
            task_state = boot.state
            for ev in boot.events:
                yield ev

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
        context_attributions: list[ContextAttribution] = []

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

        # ContextCompiler owns authority ordering, hard budgets, and attribution.
        context_blocks: list[ContextBlock] = [
            ContextBlock(
                source="kernel_policy",
                role="system",
                content=SYSTEM_POLICY,
                reason="kernel execution and safety contract",
                score=100.0,
                required=True,
                trust="kernel",
                verbatim=True,
            )
        ]
        extra_sys = (self.extra_system_prompt or "").strip()
        if extra_sys:
            context_blocks.append(
                ContextBlock(
                    source="host_policy",
                    role="system",
                    content=extra_sys,
                    reason="host-injected workspace policy",
                    score=95.0,
                    required=True,
                    trust="host",
                    verbatim=True,
                )
            )
        if task_mode and self.task_controller is not None:
            if task_state is None:
                task_content = self.task_controller.plan_prompt(prompt)
            else:
                task_content = self.task_controller.format_context(task_state)
            context_blocks.append(
                ContextBlock(
                    source="task_state",
                    role="system",
                    content=task_content,
                    reason="active task goal, step, and verification state",
                    score=90.0,
                    required=True,
                    trust="kernel_state",
                    verbatim=True,
                )
            )
        if memory_system:
            context_blocks.append(
                ContextBlock(
                    source="memory_context",
                    role="system",
                    content=memory_system,
                    reason="layered state, curated memory, summaries, and recall",
                    score=80.0,
                    required=False,
                    trust="memory_derived",
                )
            )

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
        context_blocks.append(
            ContextBlock(
                source="skill_plan",
                role="system",
                content=plan_text,
                reason="ranked procedural guidance discovery",
                score=65.0,
                required=False,
                trust="skill_registry",
            )
        )
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
                context_blocks.append(
                    ContextBlock(
                        source=f"skill_body:{skill.name}",
                        role="system",
                        content=(
                            f"[SKILL_BODY name={skill.name} score={score:.3g} "
                            f"scope=this_turn skipped=missing_tools]\n"
                            f"requires_tools missing: {', '.join(missing)}. "
                            "Register/enable those tools before following this skill."
                        ),
                        reason="selected skill cannot run without required tools",
                        score=60.0 + float(score),
                        trust="skill_registry",
                    )
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
            context_blocks.append(
                ContextBlock(
                    source=f"skill_body:{skill.name}",
                    role="system",
                    content=(
                        f"[SKILL_BODY name={skill.name} score={score:.3g} scope=this_turn]\n"
                        f"{body}"
                    ),
                    reason="auto-loaded procedural guidance",
                    score=60.0 + float(score),
                    trust="skill_registry",
                )
            )
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
            skill_events.append(
                SkillEvent(
                    kind="load",
                    skill_name=skill.name,
                    detail=f"auto_load score={score:.3g}",
                    content_digest=digest,
                )
            )
            yield TurnEvent(
                "skill_event",
                {
                    "kind": "load",
                    "skill_name": skill.name,
                    "detail": f"auto_load score={score:.3g}",
                    "content_digest": digest,
                },
            )

        # Short tool catalog (discovery); full schemas go on the wire separately.
        context_blocks.append(
            ContextBlock(
                source="tool_catalog",
                role="system",
                content="Available tools (catalog):\n" + (catalog or "(none)"),
                reason="discoverable capabilities from the single registry",
                score=75.0,
                required=True,
                trust="kernel_registry",
                verbatim=True,
            )
        )

        context_blocks.append(
            ContextBlock(
                source="runtime_context",
                role="system",
                content=(
                    "[RUNTIME_CONTEXT]\n"
                    f"now_utc: {started_at.isoformat()}\n"
                    f"session_id: {session_id}\n"
                    f"turn_id: {turn_id}"
                ),
                reason="current execution identity and time",
                score=85.0,
                required=True,
                trust="kernel_runtime",
                verbatim=True,
            )
        )

        for prior_index, prior in enumerate(
            self.memory.recent_messages(session_id=session_id)
        ):
            context_blocks.append(
                ContextBlock(
                    source=f"recent_raw:{prior_index}",
                    role=prior["role"],
                    content=prior.get("content", ""),
                    reason="recent authoritative conversation window",
                    score=50.0 + prior_index / 1000,
                    required=False,
                    trust="conversation",
                    tool_call_id=prior.get("tool_call_id"),
                    name=prior.get("name"),
                    tool_calls=prior.get("tool_calls"),
                )
            )

        user_content = build_user_message_content(prompt, image_list or None)
        user_transcript = transcript_user_line(prompt, image_list or None)
        context_blocks.append(
            ContextBlock(
                source="user_input",
                role="user",
                content=user_content,
                reason="current user request",
                score=100.0,
                required=True,
                trust="user",
                verbatim=True,
            )
        )
        request_reserved_chars = max(
            self.context_compiler.serialized_chars(candidate)
            for candidate in (
                exposure.request_tools or [],
                [SUBMIT_TASK_PLAN_TOOL],
                [REVISE_TASK_PLAN_TOOL],
            )
        )
        compiled_context = self.context_compiler.compile(
            context_blocks,
            reserved_chars=request_reserved_chars,
        )
        messages = compiled_context.messages
        context_attributions = compiled_context.attributions

        def append_required_context(
            message: dict[str, Any],
            *,
            source: str,
            reason: str,
            trust: str,
        ) -> None:
            self.context_compiler.append_required(
                messages=messages,
                attributions=context_attributions,
                block=ContextBlock(
                    source=source,
                    role=message["role"],
                    content=message.get("content"),
                    reason=reason,
                    score=100.0,
                    required=True,
                    trust=trust,
                    verbatim=True,
                    tool_call_id=message.get("tool_call_id"),
                    name=message.get("name"),
                    tool_calls=message.get("tool_calls"),
                ),
                reserved_chars=request_reserved_chars,
            )

        self.memory.transcript.append(
            {
                "role": "user",
                "content": user_transcript,
                "turn_id": turn_id,
                "session_id": session_id,
            }
        )

        evidence_parts = [user_transcript]
        observed_evidence_parts = [user_transcript]
        if self.runtime_agent is not None and hasattr(self.runtime_agent, "bind"):
            self.runtime_agent.bind(sandbox)
        ctx = ToolContext(
            session_id=session_id,
            turn_id=turn_id,
            sandbox=sandbox,
            memory=self.memory,
            skills=self.skills,
            exposure=exposure,
            skill_events=skill_events,
            evidence_text=user_transcript,
            observed_evidence_text=user_transcript,
            user_text=user_transcript,
            approval_hook=self.approval_hook,
            runtime_agent=self.runtime_agent,
            user_id=user_id,
            available_credentials=self.available_credentials,
        )

        # Inspect-only thrash: many reads without writes → nudge then force wrap-up.
        _inspect_tools = {
            "sandbox_read_file",
            "sandbox_list_dir",
            "search_skills",
            "tool_search",
            "memory",
        }
        recent_tool_names: list[str] = []
        thrash_nudge_sent = False
        model_max_tokens = max(256, int(self.max_tokens or 8192))
        skill_outcomes_recorded = False
        task_attempt_outcomes: list[dict[str, Any]] = []
        pending_skill_adoptions: dict[str, SkillEvent] = {}
        attempt_skill_events: dict[str, dict[str, SkillEvent]] = {}

        def bind_skill_event(
            event: SkillEvent,
            *,
            task_id: str,
            step_id: str,
            attempt_id: str,
        ) -> None:
            event.task_id = task_id
            event.step_id = step_id
            event.attempt_id = attempt_id
            attempt_skill_events.setdefault(attempt_id, {})[event.skill_name] = event

        def bind_pending_skills(
            *, task_id: str, step_id: str, attempt_id: str
        ) -> None:
            for event in list(pending_skill_adoptions.values()):
                bind_skill_event(
                    event,
                    task_id=task_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                )
            pending_skill_adoptions.clear()

        def record_skill_outcomes(turn_outcome: str) -> None:
            nonlocal skill_outcomes_recorded
            ledger = getattr(self.skills, "outcome_ledger", None)
            if ledger is None or skill_outcomes_recorded:
                return
            skill_outcomes_recorded = True
            candidates = [
                (skill.name, float(score))
                for skill, score in [
                    *skill_plan.get("auto_load", []),
                    *skill_plan.get("recommended", []),
                ]
            ]
            loaded = {
                event.skill_name
                for event in skill_events
                if event.kind == "load"
                and event.skill_name
                and "skipped" not in event.detail
            }
            adopted = {
                event.skill_name
                for event in skill_events
                if event.kind == "adopt" and event.skill_name
            }
            step_outcome = ""
            task_outcome = ""
            outcome_task_id = ""
            outcome_step_id = ""
            outcome_attempt_id = ""
            if task_state is not None:
                task_outcome = task_state.status
            if task_attempt_outcomes:
                latest_outcome = task_attempt_outcomes[-1]
                outcome_task_id = latest_outcome["task_id"]
                outcome_step_id = latest_outcome["step_id"]
                outcome_attempt_id = latest_outcome["attempt_id"]
                step_outcome = latest_outcome["step_outcome"]
            skill_attributions: dict[str, dict[str, Any]] = {}
            for attempt_outcome in task_attempt_outcomes:
                for skill_name in attempt_outcome.get("skills") or []:
                    skill_attributions[str(skill_name)] = {
                        "task_id": attempt_outcome["task_id"],
                        "step_id": attempt_outcome["step_id"],
                        "attempt_id": attempt_outcome["attempt_id"],
                        "step_outcome": attempt_outcome["step_outcome"],
                        "task_outcome": attempt_outcome["task_outcome"],
                        "tool_names": attempt_outcome.get("tool_names") or [],
                    }
            ledger.record_turn(
                turn_id=turn_id,
                session_id=session_id,
                candidates=candidates,
                loaded=loaded,
                adopted=adopted,
                tool_names=[call.name for call in tool_calls if call.name],
                turn_outcome=turn_outcome,
                step_outcome=step_outcome,
                task_outcome=task_outcome,
                task_id=outcome_task_id,
                step_id=outcome_step_id,
                attempt_id=outcome_attempt_id,
                skill_attributions=skill_attributions,
                user_corrected=bool((metadata or {}).get("user_corrected", False)),
            )

        try:
            for loop_i in range(loop_limit):
                if task_mode:
                    # Planning is a kernel control exchange: capabilities remain
                    # unavailable until a valid, persisted plan exists.
                    tools_payload = select_task_tools_payload(
                        task_state=task_state,
                        exposure_tools=exposure.request_tools or None,
                    )
                else:
                    tools_payload = exposure.request_tools or None
                # Last loops: force a final answer, no more tools (stop endless fix loops).
                force_final = loop_i >= max(1, loop_limit - 2)
                if force_final:
                    tools_payload = None
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "[WRAP_UP] 工具次数快用完了。请用简短中文直接告诉用户："
                                "做到哪了、能不能用、还缺啥。不要再调用工具。"
                            ),
                        }
                    )
                elif thrash_nudge_sent is False and loop_i >= 6 and len(recent_tool_names) >= 6:
                    last6 = recent_tool_names[-6:]
                    # sandbox_exec is often used for grep/wc/python checks (thrash).
                    inspect_only = all(
                        n in _inspect_tools or n == "sandbox_exec" for n in last6
                    ) and not any(
                        n in {"sandbox_write_file", "sandbox_edit_file"} for n in last6
                    )
                    if inspect_only:
                        thrash_nudge_sent = True
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "[ANTI_THRASH] 你已经连续多轮只读/检查文件，没有稳定交付。"
                                    "请立刻：要么用 sandbox_write_file 做小改动并完成，"
                                    "要么停下来用中文告诉用户进度与问题（不要再空转检查）。"
                                    "禁止一次写入超长整文件；优先小步修改。"
                                ),
                            }
                        )

                self.context_compiler.ensure_request_fits(
                    messages=messages,
                    tools=tools_payload,
                )

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
                        messages=messages,
                        tools=tools_payload,
                        model=model,
                        max_tokens=model_max_tokens,
                    ):
                        if sev.kind == "thinking_delta" and sev.text:
                            yield TurnEvent(
                                "model_thinking_delta", {"text": sev.text}
                            )
                        elif sev.kind == "delta" and sev.text:
                            yield TurnEvent("model_delta", {"text": sev.text})
                        if sev.kind == "completed" and sev.exchange is not None:
                            exchange = sev.exchange
                    if exchange is None:
                        exchange = await self.model.complete(
                            messages=messages,
                            tools=tools_payload,
                            model=model,
                            max_tokens=model_max_tokens,
                        )
                else:
                    exchange = await self.model.complete(
                        messages=messages,
                        tools=tools_payload,
                        model=model,
                        max_tokens=model_max_tokens,
                    )
                    # Non-stream: surface reasoning once if the provider returned it.
                    reasoning = getattr(exchange.message, "reasoning_content", "") or ""
                    if reasoning:
                        yield TurnEvent("model_thinking_delta", {"text": reasoning})

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
                append_required_context(
                    assistant_msg,
                    source=f"assistant_exchange:{loop_i}",
                    reason="model response and capability-call protocol",
                    trust="model",
                )
                if assistant.content:
                    evidence_parts.append(assistant.content)
                    ctx.evidence_text = "\n".join(evidence_parts)

                if task_mode and payload_has_tool(
                    tool_calls_payload, SUBMIT_TASK_PLAN_NAME
                ):
                    if self.task_controller is None:
                        raise AriadneError(
                            app_error(
                                "ARIADNE_TASK_UNAVAILABLE",
                                "TaskController is unavailable",
                            )
                        )
                    control = apply_submit_task_plan(
                        controller=self.task_controller,
                        tool_calls_payload=tool_calls_payload,
                        task_state=task_state,
                        session_id=session_id,
                        user_id=user_id,
                        original_user_goal=prompt,
                        task_mode_reason=task_mode_reason,
                    )
                    task_state = control.state
                    # The task controller is the Host authority for the
                    # immutable task→goal relation. Persist it before the
                    # model can execute or complete any task step; memory
                    # capture must never infer this relation from model text.
                    state_store = getattr(self.memory, "state", None)
                    if state_store is not None:
                        state_store.bind_task_goal(
                            session_id=session_id,
                            task_id=task_state.task_id,
                            goal_id=f"goal:{turn_id}",
                            source_turn_id=turn_id,
                            evidence_text=prompt,
                            idempotency_key=f"{turn_id}:task-goal:{task_state.task_id}",
                        )
                    for app in control.appends:
                        append_required_context(
                            app.message,
                            source=app.source,
                            reason=app.reason,
                            trust=app.trust,
                        )
                    for ev in control.events:
                        yield ev
                    continue

                if task_mode and payload_has_tool(
                    tool_calls_payload, REVISE_TASK_PLAN_NAME
                ):
                    if self.task_controller is None or task_state is None:
                        raise AriadneError(
                            app_error(
                                "ARIADNE_TASK_UNAVAILABLE",
                                "TaskController is unavailable",
                            )
                        )
                    control = apply_revise_task_plan(
                        controller=self.task_controller,
                        tool_calls_payload=tool_calls_payload,
                        task_state=task_state,
                    )
                    task_state = control.state
                    for app in control.appends:
                        append_required_context(
                            app.message,
                            source=app.source,
                            reason=app.reason,
                            trust=app.trust,
                        )
                    for ev in control.events:
                        yield ev
                    continue

                if not tool_calls_payload:
                    text = (assistant.content or "").strip()
                    if not text:
                        text = _recover_empty_assistant_text(
                            reasoning=getattr(assistant, "reasoning_content", "") or "",
                            tool_calls=tool_calls,
                        )
                        if text:
                            yield TurnEvent(
                                "model_delta",
                                {
                                    "text": text,
                                    "recovered_empty_content": True,
                                },
                            )
                    if self.guardrails_enabled:
                        text, out_findings = scan_output(text)
                        for finding in out_findings:
                            yield TurnEvent(
                                "guard_finding",
                                {"direction": "out", "kind": finding.kind, "detail": finding.detail},
                            )
                    turn_status = "completed"
                    turn_error = None
                    if task_mode:
                        if self.task_controller is None:
                            turn_status = "failed"
                            turn_error = app_error(
                                "ARIADNE_TASK_PLAN_REQUIRED",
                                "task mode requires submit_task_plan before an answer",
                            )
                        else:
                            (
                                turn_status,
                                turn_error,
                                task_state,
                                needs_ev,
                            ) = resolve_final_answer_status(
                                controller=self.task_controller,
                                state=task_state,
                                assistant_text=text or "",
                            )
                            if needs_ev is not None:
                                yield needs_ev
                    self.memory.transcript.append(
                        {
                            "role": "assistant",
                            "content": text,
                            "turn_id": turn_id,
                            "session_id": session_id,
                        }
                    )
                    # L1: widen summary input — user + assistant conclusion + truncated tools
                    tool_blob = "\n".join(
                        f"{c.name}: {json.dumps(redact_secrets(c.output), ensure_ascii=False)[:300]}"
                        for c in tool_calls
                        if c.status == "completed"
                    )
                    summary_parts = [
                        f"user: {prompt[:600]}",
                        f"assistant: {(text or '')[:800]}",
                    ]
                    if tool_blob.strip():
                        summary_parts.append(f"tools:\n{tool_blob[:1200]}")
                    source_for_summary = "\n".join(summary_parts).strip()
                    if not source_for_summary:
                        source_for_summary = f"user asked: {prompt[:200]}"
                    self.memory.summaries.enqueue(
                        session_id=session_id,
                        turn_id=turn_id,
                        source_text=source_for_summary,
                    )
                    self.memory.summaries.process_pending(session_id=session_id, max_jobs=4)
                    summary = source_for_summary[:400]
                    # Tag only entities mentioned this turn (not the entire state set).
                    evidence_blob = "\n".join(
                        [prompt, text, tool_blob]
                    )
                    entity_ids = self.memory.entities_mentioned_in_text(
                        session_id, evidence_blob
                    )
                    index_turn = getattr(self.memory, "index_turn", None)
                    if callable(index_turn):
                        index_turn(
                            session_id=session_id,
                            turn_id=turn_id,
                            user_text=prompt,
                            assistant_text=text,
                            tool_text=tool_blob,
                            summary_text=summary,
                            entity_ids=entity_ids,
                        )
                    else:
                        self.memory.semantic.index_turn(
                            session_id=session_id,
                            turn_id=turn_id,
                            user_text=prompt,
                            assistant_text=text,
                            tool_text=tool_blob,
                            summary_text=summary,
                            entity_ids=entity_ids,
                        )
                    # Memory intelligence runs at the one completed-turn write
                    # boundary. It is optional/background cognition: failures
                    # are observable, but never rewrite an already determined
                    # user-task result.
                    capture = getattr(self.memory, "capture_turn", None)
                    if callable(capture):
                        try:
                            verified_goal = None
                            if task_state is not None and task_state.status == "completed":
                                required_check_ids = {
                                    check.check_id
                                    for check in task_state.goal_checks
                                    if check.required
                                }
                                passed_check_ids = {
                                    result.check_id
                                    for result in task_state.goal_check_results
                                    if result.status == "pass"
                                }
                                if required_check_ids and required_check_ids <= passed_check_ids:
                                    verified_goal = {
                                        "status": "completed",
                                        "task_id": task_state.task_id,
                                        "goal": task_state.goal,
                                        "summary": f"verified goal completed: {task_state.goal}",
                                        "check_ids": sorted(required_check_ids),
                                    }
                            capture_report = await capture(
                                session_id=session_id,
                                turn_id=turn_id,
                                user_text=prompt,
                                assistant_text=text,
                                tool_calls=tool_calls,
                                verified_goal=verified_goal,
                            )
                            capture_status = str(
                                capture_report.get("status") or "skipped"
                            )
                            current_capture_status = str(
                                capture_report.get("capture_status")
                                or capture_status
                            )
                            recovery_failures = capture_report.get(
                                "recovery_failures"
                            ) or []
                            if not isinstance(recovery_failures, list):
                                raise AriadneError(
                                    app_error(
                                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                                        "automatic memory recovery failures must be a list",
                                    )
                                )
                            if capture_status not in {
                                "used",
                                "skipped",
                                "disabled",
                                "failed",
                            } or current_capture_status not in {
                                "used",
                                "skipped",
                                "disabled",
                            }:
                                raise AriadneError(
                                    app_error(
                                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                                        "automatic memory capture returned an unknown status",
                                        status=capture_status,
                                    )
                                )
                            if (capture_status == "failed") != bool(
                                recovery_failures
                            ):
                                raise AriadneError(
                                    app_error(
                                        "ARIADNE_MEMORY_CAPTURE_PROTOCOL",
                                        "failed capture status must correspond to reported recovery failures",
                                        status=capture_status,
                                    )
                                )
                            capture_ids = [
                                str(item)
                                for key in (
                                    "episode_id",
                                    "state_version",
                                    "user_model_entry_ids",
                                    "reflection_candidate_ids",
                                    "prospective_entry_ids",
                                    "triggered_prospective_ids",
                                    "recovered_capture_ids",
                                    "migration_required_capture_ids",
                                )
                                for item in (
                                    capture_report.get(key)
                                    if isinstance(capture_report.get(key), list)
                                    else [capture_report.get(key)]
                                )
                                if item
                            ]
                            recovery_codes = sorted(
                                {
                                    str(row.get("error_code") or "unknown")
                                    for row in recovery_failures
                                    if isinstance(row, dict)
                                }
                            )
                            capture_notes = (
                                f"capture={current_capture_status}; "
                                f"llm_used={bool(capture_report.get('llm_used'))}; "
                                f"llm_rejected={int(capture_report.get('llm_rejected') or 0)}; "
                                f"recovered={len(capture_report.get('recovered_capture_ids') or [])}; "
                                f"recovery_failed={len(recovery_failures)}; "
                                f"migration_required={len(capture_report.get('migration_required_capture_ids') or [])}"
                            )
                            if recovery_codes:
                                capture_notes += "; recovery_codes=" + ",".join(
                                    recovery_codes
                                )
                            capture_layer = LayerReport(
                                name="auto_capture",
                                status=capture_status,  # type: ignore[arg-type]
                                item_ids=capture_ids,
                                notes=capture_notes,
                            )
                        except Exception as exc:  # noqa: BLE001 - fail visible, non-fatal
                            capture_error_note = (
                                f"{exc.error.code}: {exc.error.message}"
                                if isinstance(exc, AriadneError)
                                else f"{type(exc).__name__}: {exc}"
                            )
                            capture_layer = LayerReport(
                                name="auto_capture",
                                status="failed",
                                notes=capture_error_note[:200],
                            )
                        memory_summary.layers.append(capture_layer)
                        yield TurnEvent(
                            "memory_layer",
                            {
                                "name": capture_layer.name,
                                "status": capture_layer.status,
                                "token_chars": capture_layer.token_chars,
                                "item_ids": capture_layer.item_ids,
                                "notes": capture_layer.notes,
                            },
                        )
                    # enqueue projection job only when a real projection worker is wired
                    if getattr(self.memory, "projection", None) is not None:
                        self.memory.projection.enqueue(
                            session_id=session_id,
                            turn_id=turn_id,
                            evidence_text="\n".join(evidence_parts)[:8000],
                        )
                        if self.memory_projector is not None:
                            await self.memory.projection.process_one(
                                self.memory_projector,
                                worker_id="turn_application",
                                session_id=session_id,
                            )
                    # Skill digest pins for audit/replay (name → content_digest)
                    skill_pins: dict[str, str] = {}
                    for ev in skill_events:
                        if (
                            getattr(ev, "kind", "") == "load"
                            and getattr(ev, "skill_name", "")
                            and getattr(ev, "content_digest", "")
                        ):
                            skill_pins[str(ev.skill_name)] = str(ev.content_digest)
                    record_skill_outcomes(turn_status)
                    await guard.release()
                    result = TurnResult(
                        turn_id=turn_id,
                        status=turn_status,  # type: ignore[arg-type]
                        text=text,
                        messages=_public_messages(messages),
                        tool_calls=tool_calls,
                        skill_events=skill_events,
                        memory=memory_summary,
                        usage=usage_total,
                        session_id=session_id,
                        model=model or self.model.model,
                        schema_metrics=schema_metrics,
                        skill_pins=skill_pins,
                        task=TaskSummary.from_state(task_state) if task_state else None,
                        error=turn_error,
                        context_attributions=list(context_attributions),
                    )
                    yield TurnEvent(
                        "turn_failed" if turn_status == "failed" else "turn_completed",
                        {"result": result},
                    )
                    return

                task_attempt_id = ""
                task_attempt_spec = None
                task_attempt_effect = "unknown"
                if task_mode:
                    if self.task_controller is None or task_state is None:
                        raise AriadneError(
                            app_error(
                                "ARIADNE_TASK_PLAN_REQUIRED",
                                "capability calls require a persisted task plan",
                            )
                        )
                    cap_plan = prepare_capability_exchange(
                        controller=self.task_controller,
                        tools=self.tools,
                        state=task_state,
                        tool_calls_payload=tool_calls_payload,
                    )
                    task_state = cap_plan.state
                    task_attempt_id = cap_plan.attempt_id
                    task_attempt_spec = cap_plan.attempt_spec
                    task_attempt_effect = cap_plan.attempt_effect
                    if task_attempt_id:
                        bind_pending_skills(
                            task_id=task_state.task_id,
                            step_id=str(task_state.current_step_id or ""),
                            attempt_id=task_attempt_id,
                        )
                    if cap_plan.step_started_event is not None:
                        yield cap_plan.step_started_event

                exchange = await invoke_tool_exchange(
                    tools=self.tools,
                    tool_calls_payload=tool_calls_payload,
                    ctx=ctx,
                    redact_traces=self.redact_traces,
                    task_id=task_state.task_id if task_state else "",
                    step_id=(task_state.current_step_id or "") if task_state else "",
                    attempt_id=task_attempt_id,
                    skill_events=skill_events,
                    pending_skill_adoptions=pending_skill_adoptions,
                    bind_skill_event=bind_skill_event,
                )
                for name in exchange.tool_names:
                    recent_tool_names.append(name)
                if len(recent_tool_names) > 24:
                    recent_tool_names = recent_tool_names[-24:]
                for app in exchange.appends:
                    append_required_context(
                        app.message,
                        source=app.source,
                        reason=app.reason,
                        trust=app.trust,
                    )
                for snippet in exchange.evidence_snippets:
                    evidence_parts.append(snippet)
                ctx.evidence_text = "\n".join(evidence_parts)
                for snippet in exchange.observed_evidence_snippets:
                    observed_evidence_parts.append(snippet)
                ctx.observed_evidence_text = "\n".join(observed_evidence_parts)
                tool_calls.extend(exchange.traces)
                for ev in exchange.events:
                    yield ev

                if (
                    task_mode
                    and task_attempt_id
                    and self.task_controller is not None
                    and task_state is not None
                ):
                    finalized = await finalize_attempt(
                        controller=self.task_controller,
                        state=task_state,
                        traces=exchange.traces,
                        attempt_spec=task_attempt_spec,
                        attempt_effect=task_attempt_effect,
                        attempt_id=task_attempt_id,
                        skill_names=sorted(
                            attempt_skill_events.get(task_attempt_id, {}).keys()
                        ),
                    )
                    task_state = finalized.state
                    task_attempt_outcomes.append(finalized.outcome_row)
                    for ev in finalized.events:
                        yield ev
                    append_required_context(
                        {
                            "role": "system",
                            "content": finalized.context_system_text,
                        },
                        source=f"task_state_revision:{task_state.revision}",
                        reason="authoritative task state after verification",
                        trust="kernel_state",
                    )

            await guard.release()
            err = app_error(
                "ARIADNE_TOOL_LOOP_LIMIT",
                f"Exceeded tool loop limit ({loop_limit})",
            )
            tool_summary = "、".join(
                dict.fromkeys(c.name for c in tool_calls if c.name)  # unique, order-preserving
            ) or "（无）"
            limit_text = (
                f"这轮干得太久了（工具循环到上限 {loop_limit}），我先停一下，避免一直转圈。\n\n"
                f"本轮用过：{tool_summary}\n\n"
                "你可以：\n"
                "1. 再说「继续」——我接着改\n"
                "2. 把任务说得更小一点（例如只改一个文件）\n"
                "3. 问我「做到哪了」——我先汇报现状\n\n"
                "小提示：一次别让我写超大文件，容易写一半卡住。"
            )
            self.memory.transcript.append(
                {
                    "role": "assistant",
                    "content": limit_text,
                    "turn_id": turn_id,
                    "session_id": session_id,
                }
            )
            record_skill_outcomes("failed")
            result = TurnResult(
                turn_id=turn_id,
                status="failed",
                text=limit_text,
                messages=_public_messages(messages),
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=err,
                session_id=session_id,
                model=model or self.model.model,
                schema_metrics=schema_metrics,
                task=TaskSummary.from_state(task_state) if task_state else None,
                context_attributions=list(context_attributions),
            )
            yield TurnEvent("turn_failed", {"result": result})
        except AriadneError as exc:
            await guard.release()
            record_skill_outcomes("failed")
            needs_input = bool(task_state is not None and task_state.status == "needs_input")
            if needs_input:
                yield TurnEvent(
                    "task_needs_input",
                    {
                        "task_id": task_state.task_id,
                        "current_step_id": task_state.current_step_id,
                        "question": (
                            task_state.open_questions[0].prompt
                            if task_state.open_questions
                            else exc.error.message
                        ),
                    },
                )
            result = TurnResult(
                turn_id=turn_id,
                status="needs_input" if needs_input else "failed",
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
                task=TaskSummary.from_state(task_state) if task_state else None,
                context_attributions=list(context_attributions),
            )
            yield TurnEvent("turn_completed" if needs_input else "turn_failed", {"result": result})
        except Exception as exc:  # noqa: BLE001
            await guard.release()
            record_skill_outcomes("failed")
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
                task=TaskSummary.from_state(task_state) if task_state else None,
                context_attributions=list(context_attributions),
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
