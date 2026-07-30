from __future__ import annotations

import json
from typing import Any

from ..types import TurnEvent, TurnResult


def render_event(event: TurnEvent, *, verbose: bool = False) -> str:
    if event.kind == "model_delta":
        return str(event.data.get("text") or "")
    if event.kind == "model_thinking_delta":
        # Optional verbose: prefix so hosts can filter thinking from answer.
        if verbose:
            return str(event.data.get("text") or "")
        return ""
    if not verbose:
        return ""
    if event.kind == "turn_started":
        return f"[turn {event.data.get('turn_id')} started]\n"
    if event.kind == "tool_started":
        return f"• tool {event.data.get('name')} …\n"
    if event.kind == "tool_completed":
        status = event.data.get("status")
        mark = "•" if status == "completed" else "×"
        return f"{mark} tool {event.data.get('name')} {status}\n"
    if event.kind == "skill_event":
        return f"skill {event.data.get('kind')}: {event.data.get('skill_name') or event.data.get('detail')}\n"
    if event.kind == "memory_layer":
        return f"memory {event.data.get('name')}={event.data.get('status')}\n"
    return ""


def render_human(
    result: TurnResult,
    *,
    verbose: bool = False,
    skip_text: bool = False,
) -> str:
    lines: list[str] = []
    if verbose or result.tool_calls:
        for call in result.tool_calls:
            mark = "•" if call.status == "completed" else "×"
            lines.append(f"{mark} tool {call.name}")
            cmd = call.arguments.get("cmd") if isinstance(call.arguments, dict) else None
            if cmd:
                lines.append(f"  $ {cmd}")
            if call.status == "failed" and call.error is not None:
                lines.append(f"  error {call.error.code}: {call.error.message}")
                continue
            out = call.output
            if isinstance(out, dict):
                if out.get("stdout"):
                    for ln in str(out["stdout"]).rstrip().splitlines()[:40]:
                        lines.append(f"  {ln}")
                    more = str(out["stdout"]).count("\n") + 1
                    if more > 40:
                        lines.append(f"  … ({more - 40} more stdout lines)")
                if out.get("stderr"):
                    lines.append("  [stderr]")
                    for ln in str(out["stderr"]).rstrip().splitlines()[:20]:
                        lines.append(f"  {ln}")
                if "exit_code" in out:
                    extra = []
                    if out.get("timed_out"):
                        extra.append("timed_out")
                    if out.get("truncated"):
                        extra.append("truncated")
                    if out.get("compressed"):
                        extra.append("compressed")
                    suffix = f" ({', '.join(extra)})" if extra else ""
                    lines.append(f"  exit {out['exit_code']}{suffix}")
            elif out is not None:
                lines.append(f"  {out}")
        if lines:
            lines.append("")
    if result.status == "failed":
        err = result.error
        if err is not None:
            lines.append(f"ERROR {err.code}: {err.message}")
        else:
            lines.append("ERROR: turn failed")
    elif not skip_text:
        lines.append(result.text or "")
    if verbose and result.usage.total_tokens:
        lines.append("")
        lines.append(
            f"[usage prompt={result.usage.prompt_tokens} completion={result.usage.completion_tokens} "
            f"total={result.usage.total_tokens} reasoning={result.usage.reasoning_tokens}]"
        )
    if verbose and result.schema_metrics:
        last = result.schema_metrics[-1]
        lines.append(
            f"[schema tools={last.tool_count} schema_chars={last.schema_chars} "
            f"catalog_chars={last.catalog_chars} deferred={last.deferred_count}]"
        )
    return "\n".join(lines).rstrip() + "\n"


def render_json(result: TurnResult) -> str:
    def default(obj: Any) -> Any:
        if hasattr(obj, "__dict__"):
            return {k: getattr(obj, k) for k in obj.__dataclass_fields__}  # type: ignore[attr-defined]
        return str(obj)

    payload = {
        "turn_id": result.turn_id,
        "status": result.status,
        "text": result.text,
        "session_id": result.session_id,
        "model": result.model,
        "messages": [
            {
                "role": m.role,
                "content": m.content,
                **({"tool_call_id": m.tool_call_id} if m.tool_call_id else {}),
                **({"name": m.name} if m.name else {}),
                **({"tool_calls": m.tool_calls} if m.tool_calls else {}),
            }
            for m in result.messages
        ],
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
            "reasoning_tokens": result.usage.reasoning_tokens,
        },
        "schema_metrics": [
            {
                "exchange_index": m.exchange_index,
                "tool_count": m.tool_count,
                "schema_chars": m.schema_chars,
                "catalog_chars": m.catalog_chars,
                "deferred_count": m.deferred_count,
                "loaded_deferred": m.loaded_deferred,
            }
            for m in result.schema_metrics
        ],
        "skill_events": [
            {
                "kind": e.kind,
                "skill_name": e.skill_name,
                "detail": e.detail,
                "content_digest": getattr(e, "content_digest", "") or "",
            }
            for e in result.skill_events
        ],
        "skill_pins": dict(getattr(result, "skill_pins", None) or {}),
        "context_attributions": [
            {
                "source": item.source,
                "reason": item.reason,
                "score": item.score,
                "token_chars": item.token_chars,
                "disposition": item.disposition,
                "role": item.role,
                "trust": item.trust,
                "required": item.required,
                "verbatim": item.verbatim,
            }
            for item in getattr(result, "context_attributions", [])
        ],
        "task": (
            {
                key: getattr(result.task, key)
                for key in result.task.__dataclass_fields__
            }
            if getattr(result, "task", None) is not None
            else None
        ),
        "memory": {
            "curated_count": result.memory.curated_count,
            "state_entity_count": result.memory.state_entity_count,
            "recent_turn_count": result.memory.recent_turn_count,
            "layers": [
                {
                    "name": layer.name,
                    "status": layer.status,
                    "token_chars": layer.token_chars,
                    "item_ids": layer.item_ids,
                    "notes": layer.notes,
                }
                for layer in result.memory.layers
            ],
        },
        "tool_calls": [
            {
                "call_id": c.call_id,
                "name": c.name,
                "arguments": c.arguments,
                "output": c.output,
                "status": c.status,
                "schema_chars": c.schema_chars,
                "error": None
                if c.error is None
                else {
                    "code": c.error.code,
                    "message": c.error.message,
                    "details": c.error.details,
                },
            }
            for c in result.tool_calls
        ],
        "error": None
        if result.error is None
        else {
            "code": result.error.code,
            "message": result.error.message,
            "details": result.error.details,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=default) + "\n"
