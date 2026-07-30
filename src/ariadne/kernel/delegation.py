from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..errors import AriadneError, app_error
from ..model.base import ModelPort
from ..tools.registry import ToolContext, ToolSpec

DELEGATE_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_delegate_report",
        "description": "Return one evidence-grounded advisory report for the assigned subgoal.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["conclusion", "evidence_quote", "confidence"],
            "properties": {
                "conclusion": {"type": "string", "minLength": 1},
                "evidence_quote": {"type": "string", "minLength": 1},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
}


@dataclass(slots=True)
class ControlledDelegator:
    """Bounded advisory fan-out: delegates get evidence, never capabilities."""

    model: ModelPort
    max_parallel: int = 3
    max_evidence_chars: int = 12_000

    async def run(
        self,
        *,
        subgoals: list[str],
        evidence_text: str,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        clean = [str(item).strip() for item in subgoals if str(item).strip()]
        if not 2 <= len(clean) <= self.max_parallel:
            raise AriadneError(
                app_error(
                    "ARIADNE_DELEGATION_INVALID",
                    f"controlled delegation requires 2..{self.max_parallel} subgoals",
                )
            )
        normalized = {" ".join(item.lower().split()) for item in clean}
        if len(normalized) != len(clean):
            raise AriadneError(
                app_error(
                    "ARIADNE_DELEGATION_INVALID",
                    "delegated subgoals must be independent and unique",
                )
            )
        evidence = str(evidence_text or "")
        if not evidence.strip():
            raise AriadneError(
                app_error(
                    "ARIADNE_DELEGATION_INVALID",
                    "delegation requires shared evidence from the parent turn",
                )
            )
        if len(evidence) > self.max_evidence_chars:
            raise AriadneError(
                app_error(
                    "ARIADNE_DELEGATION_INVALID",
                    "delegation evidence exceeds the hard cap",
                    evidence_chars=len(evidence),
                    max_chars=self.max_evidence_chars,
                )
            )

        async def one(index: int, subgoal: str) -> dict[str, Any]:
            try:
                exchange = await self.model.complete(
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a read-only advisory subagent. You have no tools and may "
                                "not claim actions. Analyze only the assigned subgoal from EVIDENCE, "
                                "then call submit_delegate_report exactly once."
                            ),
                        },
                        {
                            "role": "user",
                            "content": f"SUBGOAL: {subgoal}\nEVIDENCE:\n{evidence}",
                        },
                    ],
                    tools=[DELEGATE_REPORT_TOOL],
                    tool_choice={
                        "type": "function",
                        "function": {"name": "submit_delegate_report"},
                    },
                    temperature=0.0,
                    max_tokens=1200,
                    model=model,
                )
                calls = exchange.message.tool_calls or []
                if len(calls) != 1 or str((calls[0].get("function") or {}).get("name")) != (
                    "submit_delegate_report"
                ):
                    raise ValueError("delegate must return exactly one structured report")
                raw = (calls[0].get("function") or {}).get("arguments") or "{}"
                arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
                Draft202012Validator(
                    DELEGATE_REPORT_TOOL["function"]["parameters"]
                ).validate(arguments)
                quote = str(arguments["evidence_quote"])
                if quote not in evidence:
                    raise ValueError("delegate evidence_quote is not present in parent evidence")
            except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
                raise AriadneError(
                    app_error(
                        "ARIADNE_DELEGATION_PROTOCOL",
                        f"delegate {index} returned an invalid report: {exc}",
                        subgoal=subgoal,
                    )
                ) from exc
            except AriadneError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider boundary becomes structured
                raise AriadneError(
                    app_error(
                        "ARIADNE_DELEGATION_MODEL_ERROR",
                        f"delegate {index} model call failed: {type(exc).__name__}: {exc}",
                        subgoal=subgoal,
                    )
                ) from exc
            return {
                "delegate_id": f"delegate_{index + 1}",
                "subgoal": subgoal,
                "conclusion": str(arguments["conclusion"]),
                "evidence_quote": quote,
                "confidence": float(arguments["confidence"]),
                "verified": True,
                "capabilities_exposed": 0,
            }

        return list(await asyncio.gather(*(one(index, goal) for index, goal in enumerate(clean))))


def build_controlled_delegation_tool(model: ModelPort) -> ToolSpec:
    delegator = ControlledDelegator(model)

    async def handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        subgoals = args.get("subgoals") or []
        if not isinstance(subgoals, list):
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "subgoals must be an array")
            )
        reports = await delegator.run(
            subgoals=[str(item) for item in subgoals],
            evidence_text=ctx.observed_evidence_text,
        )
        return {
            "reports": reports,
            "report_count": len(reports),
            "mode": "read_only_advisory",
        }

    return ToolSpec(
        name="delegate_analysis",
        catalog_description="parallel evidence-grounded advisory analysis",
        description=(
            "Fan out 2-3 independent read-only analysis subgoals. Delegates receive no "
            "capabilities and every report must quote parent-turn evidence. The parent remains "
            "responsible for actions and task verification."
        ),
        parameters={
            "type": "object",
            "additionalProperties": False,
            "required": ["subgoals"],
            "properties": {
                "subgoals": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                }
            },
        },
        handler=handler,
        title="Delegate analysis",
        kind="tool",
        tool_exposure="named_deferred",
        side_effect_level="none",
        network_access="outbound",
        idempotent=True,
        verification_hint=("llm_semantic",),
    )
