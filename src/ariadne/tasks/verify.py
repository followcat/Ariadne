from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..errors import app_error
from ..types import ToolCallTrace
from .models import Check, CheckResult, EvidenceRef


@dataclass(slots=True)
class DeterministicVerifier:
    workspace: Path | None

    def _workspace_path(self, raw: Any) -> Path:
        if self.workspace is None:
            raise ValueError("task verifier has no workspace")
        text = str(raw or "").strip()
        if not text:
            raise ValueError("check path is required")
        root = Path(self.workspace).resolve()
        if text == "/workspace":
            candidate = root
        elif text.startswith("/workspace/"):
            candidate = root / text.removeprefix("/workspace/")
        else:
            path = Path(text)
            if path.is_absolute():
                raise ValueError("absolute paths must use the /workspace prefix")
            candidate = root / path
        resolved = candidate.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            raise ValueError("check path escapes workspace root")
        return resolved

    @staticmethod
    def _evidence(
        *, kind: str, ref: str, summary: str, attempt_id: str
    ) -> EvidenceRef:
        return EvidenceRef(
            evidence_id=f"evidence_{uuid.uuid4().hex[:12]}",
            kind=kind,  # type: ignore[arg-type]
            ref=ref,
            summary=summary,
            attempt_id=attempt_id,
        )

    def run(
        self,
        check: Check,
        *,
        traces: Iterable[ToolCallTrace],
        attempt_id: str,
        resume: bool = False,
    ) -> CheckResult:
        try:
            if check.kind == "command_exit":
                return self._command_exit(check, traces=traces, attempt_id=attempt_id, resume=resume)
            if check.kind in {"path_exists", "path_absent", "file_contains"}:
                return self._path_check(check, attempt_id=attempt_id)
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_CHECK_UNSUPPORTED",
                    f"check kind is not implemented: {check.kind}",
                    kind=check.kind,
                ),
            )
        except (OSError, UnicodeError, ValueError) as exc:
            return CheckResult(
                check_id=check.check_id,
                status="error",
                error=app_error(
                    "ARIADNE_TASK_CHECK_ERROR",
                    f"{type(exc).__name__}: {exc}",
                    kind=check.kind,
                ),
            )

    def run_many(
        self,
        checks: Iterable[Check],
        *,
        traces: Iterable[ToolCallTrace],
        attempt_id: str,
        resume: bool = False,
    ) -> list[CheckResult]:
        trace_list = list(traces)
        return [
            self.run(check, traces=trace_list, attempt_id=attempt_id, resume=resume)
            for check in checks
        ]

    def _command_exit(
        self,
        check: Check,
        *,
        traces: Iterable[ToolCallTrace],
        attempt_id: str,
        resume: bool,
    ) -> CheckResult:
        if resume:
            return CheckResult(check_id=check.check_id, status="stale")
        wanted = str(check.spec.get("tool_call_id") or "")
        candidates = [trace for trace in traces if trace.name == "sandbox_exec"]
        if wanted:
            candidates = [trace for trace in candidates if trace.call_id == wanted]
        if not candidates:
            return CheckResult(check_id=check.check_id, status="not_run")
        trace = candidates[-1]
        evidence = self._evidence(
            kind="command",
            ref=trace.call_id,
            summary=f"sandbox_exec status={trace.status}",
            attempt_id=attempt_id,
        )
        if trace.status != "completed" or not isinstance(trace.output, dict):
            return CheckResult(
                check_id=check.check_id,
                status="error",
                evidence=[evidence],
                error=trace.error
                or app_error("ARIADNE_TASK_CHECK_ERROR", "referenced command did not complete"),
            )
        actual = trace.output.get("exit_code")
        expected = int(check.spec.get("expected", 0))
        timed_out = bool(trace.output.get("timed_out", False))
        passed = actual == expected and not timed_out
        return CheckResult(
            check_id=check.check_id,
            status="pass" if passed else "fail",
            evidence=[evidence],
            observed_value={"exit_code": actual, "timed_out": timed_out},
        )

    def _path_check(self, check: Check, *, attempt_id: str) -> CheckResult:
        path = self._workspace_path(check.spec.get("path"))
        exists = path.exists()
        evidence = self._evidence(
            kind="tool_result",
            ref=str(path),
            summary=f"{check.kind} observed exists={exists}",
            attempt_id=attempt_id,
        )
        if check.kind == "path_exists":
            return CheckResult(
                check_id=check.check_id,
                status="pass" if exists else "fail",
                evidence=[evidence],
                observed_value=exists,
            )
        if check.kind == "path_absent":
            return CheckResult(
                check_id=check.check_id,
                status="pass" if not exists else "fail",
                evidence=[evidence],
                observed_value=exists,
            )
        if not exists or not path.is_file():
            return CheckResult(
                check_id=check.check_id,
                status="fail",
                evidence=[evidence],
                observed_value={"exists": exists, "contains": False},
            )
        needle = check.spec.get("text")
        if not isinstance(needle, str) or not needle:
            raise ValueError("file_contains requires non-empty spec.text")
        content = path.read_text(encoding=str(check.spec.get("encoding") or "utf-8"))
        contains = needle in content
        return CheckResult(
            check_id=check.check_id,
            status="pass" if contains else "fail",
            evidence=[evidence],
            observed_value={"exists": True, "contains": contains},
            checked_at=time.time(),
        )
