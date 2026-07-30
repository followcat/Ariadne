from __future__ import annotations

import hashlib
import struct
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
            if check.kind == "image_file":
                return self._image_file(check, attempt_id=attempt_id)
            if check.kind == "llm_semantic" and resume:
                return CheckResult(check_id=check.check_id, status="stale")
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

    @staticmethod
    def _image_info(data: bytes) -> tuple[str, int | None, int | None]:
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            return "png", width, height
        if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
            width, height = struct.unpack("<HH", data[6:10])
            return "gif", width, height
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "webp", None, None
        if data.startswith(b"\xff\xd8"):
            offset = 2
            while offset + 9 <= len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                offset += 2
                if marker in {0xD8, 0xD9}:
                    continue
                if offset + 2 > len(data):
                    break
                segment_len = int.from_bytes(data[offset : offset + 2], "big")
                if segment_len < 2 or offset + segment_len > len(data):
                    break
                if marker in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                } and segment_len >= 7:
                    height = int.from_bytes(data[offset + 3 : offset + 5], "big")
                    width = int.from_bytes(data[offset + 5 : offset + 7], "big")
                    return "jpeg", width, height
                offset += segment_len
            return "jpeg", None, None
        raise ValueError("file bytes are not a supported PNG/JPEG/GIF/WebP image")

    def _image_file(self, check: Check, *, attempt_id: str) -> CheckResult:
        path = self._workspace_path(check.spec.get("path"))
        if not path.is_file():
            return CheckResult(
                check_id=check.check_id,
                status="fail",
                observed_value={"exists": path.exists(), "valid_image": False},
            )
        data = path.read_bytes()
        image_format, width, height = self._image_info(data)
        wanted_format = str(check.spec.get("format") or "").lower().removeprefix("image/")
        if wanted_format == "jpg":
            wanted_format = "jpeg"
        min_bytes = int(check.spec.get("min_bytes") or 1)
        min_width = int(check.spec.get("min_width") or 0)
        min_height = int(check.spec.get("min_height") or 0)
        passed = len(data) >= min_bytes
        if wanted_format:
            passed = passed and image_format == wanted_format
        if min_width:
            passed = passed and width is not None and width >= min_width
        if min_height:
            passed = passed and height is not None and height >= min_height
        digest = hashlib.sha256(data).hexdigest()
        evidence = self._evidence(
            kind="image",
            ref=str(path),
            summary=(
                f"verified {image_format} image {width or '?'}x{height or '?'} "
                f"bytes={len(data)} sha256={digest}"
            ),
            attempt_id=attempt_id,
        )
        return CheckResult(
            check_id=check.check_id,
            status="pass" if passed else "fail",
            evidence=[evidence],
            observed_value={
                "path": str(path),
                "format": image_format,
                "width": width,
                "height": height,
                "bytes": len(data),
                "sha256": digest,
            },
        )
