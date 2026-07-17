from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..errors import AriadneError, app_error
from ..memory.facade import MemoryFacade
from ..model.base import ModelPort
from ..sandbox.port import SandboxBackend
from ..skills.store import SkillStore
from ..tools.registry import ToolContext, ToolRegistry, dumps_tool_output
from ..types import SkillEvent, ToolCallTrace, TurnResult, Usage


SYSTEM_POLICY = """You are Ariadne, a local shell agent working inside a user project directory.

Filesystem contract for sandbox_exec:
- Default cwd is the project root (logical name: /workspace). Prefer relative paths such as README.md or src/app.py.
- Scratch directory is logical /session (cwd="/session"); also available as $ARIADNE_SESSION_DIR.
- Do not use absolute host paths like /home/.... Stay under the project or session scratch.
- Shell variables (cd/export) do NOT persist across sandbox_exec calls. Persist state in files.

Skills:
- A short skill index may be provided. Use search_skills / load_skill when you need procedures.
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


@dataclass
class TurnApplication:
    model: ModelPort
    tools: ToolRegistry
    sandbox_backend: SandboxBackend
    memory: MemoryFacade
    skills: SkillStore
    tool_loop_limit: int = 16
    prefer_deferred_tools: bool = True

    async def run(self, *, prompt: str, session_id: str, model: str | None = None) -> TurnResult:
        turn_id = uuid.uuid4().hex[:12]
        scope_key = f"{session_id}-{turn_id}"
        sandbox = await self.sandbox_backend.start(scope_key=scope_key)
        tool_calls: list[ToolCallTrace] = []
        skill_events: list[SkillEvent] = []
        usage_total = Usage()

        memory_system, memory_summary = self.memory.build_context(session_id=session_id, query=prompt)
        skill_index = self.skills.index_text()
        exposure = self.tools.build_exposure(prefer_deferred=self.prefer_deferred_tools)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_POLICY},
            {
                "role": "system",
                "content": "Available tools (catalog):\n" + (self.tools.catalog_text() or "(none)"),
            },
        ]
        if skill_index and skill_index != "(no skills installed)":
            messages.append({"role": "system", "content": "Skill index:\n" + skill_index})
            skill_events.append(SkillEvent(kind="index", detail=f"{len(self.skills.list())} skills"))
        if memory_system:
            messages.append({"role": "system", "content": memory_system})

        for prior in self.memory.transcript.recent_messages():
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
            for _ in range(self.tool_loop_limit):
                exchange = await self.model.complete(
                    messages=messages,
                    tools=exposure.request_tools or None,
                    model=model,
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
                    # L1 summary + L4 index
                    summary = text[:400] if text else f"user asked: {prompt[:200]}"
                    self.memory.summaries.put(session_id=session_id, turn_id=turn_id, summary_text=summary)
                    tool_blob = "\n".join(
                        f"{c.name}: {json.dumps(c.output, ensure_ascii=False)[:300]}"
                        for c in tool_calls
                        if c.status == "completed"
                    )
                    self.memory.semantic.index_turn(
                        session_id=session_id,
                        turn_id=turn_id,
                        user_text=prompt,
                        assistant_text=text,
                        tool_text=tool_blob,
                    )
                    await sandbox.close(reason="turn_finished")
                    return TurnResult(
                        turn_id=turn_id,
                        status="completed",
                        text=text,
                        tool_calls=tool_calls,
                        skill_events=skill_events,
                        memory=memory_summary,
                        usage=usage_total,
                        session_id=session_id,
                        model=model or self.model.model,
                    )

                for call in tool_calls_payload:
                    call_id = str(call.get("id") or uuid.uuid4().hex)
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments") or "{}"
                    started = datetime.now(timezone.utc)
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        if not isinstance(args, dict):
                            raise ValueError("tool arguments must be a JSON object")
                        # refresh callable set for deferred tools after tool_search
                        output = await self.tools.invoke(name, args, ctx)
                        finished = datetime.now(timezone.utc)
                        tool_calls.append(
                            ToolCallTrace(
                                call_id=call_id,
                                name=name,
                                arguments=args,
                                output=output,
                                status="completed",
                                started_at=started,
                                finished_at=finished,
                            )
                        )
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call_id,
                                "content": dumps_tool_output(output),
                            }
                        )
                        evidence_parts.append(dumps_tool_output(output)[:2000])
                        ctx.evidence_text = "\n".join(evidence_parts)
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

            await sandbox.close(reason="tool_loop_limit")
            err = app_error(
                "ARIADNE_TOOL_LOOP_LIMIT",
                f"Exceeded tool loop limit ({self.tool_loop_limit})",
            )
            return TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=err,
                session_id=session_id,
                model=model or self.model.model,
            )
        except AriadneError as exc:
            await sandbox.close(reason="turn_failed")
            return TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=exc.error,
                session_id=session_id,
                model=model or self.model.model,
            )
        except Exception as exc:  # noqa: BLE001
            await sandbox.close(reason="turn_failed")
            return TurnResult(
                turn_id=turn_id,
                status="failed",
                text="",
                tool_calls=tool_calls,
                skill_events=skill_events,
                memory=memory_summary,
                usage=usage_total,
                error=app_error("ARIADNE_MODEL_ERROR", f"{type(exc).__name__}: {exc}"),
                session_id=session_id,
                model=model or self.model.model,
            )
