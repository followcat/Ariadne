from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Iterable, Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..errors import AriadneError, app_error
from ..model.base import ModelPort
from ..types import ToolCallTrace
from .models import Check, CheckResult, EvidenceRef

SEMANTIC_CHECK_TOOL = {
    "type": "function",
    "function": {
        "name": "report_semantic_check",
        "description": (
            "Judge the criterion only from supplied evidence. Quote verbatim evidence; "
            "do not infer missing facts."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "evidence_quote", "rationale"],
            "properties": {
                "status": {"type": "string", "enum": ["pass", "fail"]},
                "evidence_quote": {"type": "string", "minLength": 1},
                "rationale": {"type": "string", "minLength": 1},
            },
        },
    },
}


@dataclass(slots=True)
class SemanticVerifier:
    model: ModelPort

    async def run(
        self,
        check: Check,
        *,
        traces: Iterable[ToolCallTrace],
        attempt_id: str,
    ) -> CheckResult:
        if check.kind != "llm_semantic":
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_CHECK_UNSUPPORTED",
                    "SemanticVerifier only accepts llm_semantic checks",
                )
            )
        criterion = str(check.spec.get("criterion") or "").strip()
        oracle_reason = str(check.spec.get("oracle_unavailable_reason") or "").strip()
        if not criterion or not oracle_reason:
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_SEMANTIC_INVALID",
                    "llm_semantic requires criterion and oracle_unavailable_reason",
                ),
            )
        trace_list = list(traces)
        if not trace_list:
            return CheckResult(check_id=check.check_id, status="not_run")
        evidence_rows: list[dict[str, Any]] = []
        for trace in trace_list:
            evidence_rows.append(
                {
                    "call_id": trace.call_id,
                    "name": trace.name,
                    "status": trace.status,
                    "output": trace.output,
                    "error": (
                        {
                            "code": trace.error.code,
                            "message": trace.error.message,
                        }
                        if trace.error
                        else None
                    ),
                }
            )
        evidence_text = json.dumps(evidence_rows, ensure_ascii=False, sort_keys=True)
        try:
            exchange = await self.model.complete(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a verification judge, not an executor. Use only EVIDENCE. "
                            "Return exactly one report_semantic_check call."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"CRITERION: {criterion}\n"
                            f"WHY_NO_DETERMINISTIC_ORACLE: {oracle_reason}\n"
                            f"EVIDENCE:\n{evidence_text}"
                        ),
                    },
                ],
                tools=[SEMANTIC_CHECK_TOOL],
                tool_choice={
                    "type": "function",
                    "function": {"name": "report_semantic_check"},
                },
                temperature=0.0,
                max_tokens=800,
            )
            calls = exchange.message.tool_calls or []
            if len(calls) != 1 or str((calls[0].get("function") or {}).get("name")) != (
                "report_semantic_check"
            ):
                raise ValueError("semantic verifier must return exactly one structured call")
            raw = (calls[0].get("function") or {}).get("arguments") or "{}"
            arguments = json.loads(raw) if isinstance(raw, str) else dict(raw)
            Draft202012Validator(
                SEMANTIC_CHECK_TOOL["function"]["parameters"]
            ).validate(arguments)
            quote = str(arguments["evidence_quote"])
            if quote not in evidence_text:
                raise ValueError("semantic verifier evidence_quote is not verbatim evidence")
        except (TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_SEMANTIC_VERIFIER_ERROR",
                    f"semantic verifier protocol error: {exc}",
                ),
            )
        except AriadneError as exc:
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_SEMANTIC_VERIFIER_ERROR",
                    f"semantic verifier model error: {exc.error.code}: {exc.error.message}",
                    provider_code=exc.error.code,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - boundary maps provider failure to CHECK ERROR
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_SEMANTIC_VERIFIER_ERROR",
                    f"semantic verifier model error: {type(exc).__name__}: {exc}",
                ),
            )
        evidence = EvidenceRef(
            evidence_id=f"evidence_{uuid.uuid4().hex[:12]}",
            kind="tool_result",
            ref=trace_list[-1].call_id,
            summary=f"semantic quote: {quote}",
            attempt_id=attempt_id,
        )
        return CheckResult(
            check_id=check.check_id,
            status=str(arguments["status"]),  # type: ignore[arg-type]
            evidence=[evidence],
            observed_value={
                "criterion": criterion,
                "evidence_quote": quote,
                "rationale": str(arguments["rationale"]),
            },
        )
