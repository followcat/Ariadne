from __future__ import annotations

import json
from typing import Any

from ..types import TurnResult


def render_human(result: TurnResult, *, verbose: bool = False) -> str:
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
    else:
        lines.append(result.text or "")
    if verbose and result.usage.total_tokens:
        lines.append("")
        lines.append(
            f"[usage prompt={result.usage.prompt_tokens} completion={result.usage.completion_tokens} "
            f"total={result.usage.total_tokens} reasoning={result.usage.reasoning_tokens}]"
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
        "usage": {
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
            "reasoning_tokens": result.usage.reasoning_tokens,
        },
        "skill_events": [
            {"kind": e.kind, "skill_name": e.skill_name, "detail": e.detail}
            for e in result.skill_events
        ],
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
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
