"""Optional live-LLM closed-loop acceptance (skipped unless opted in).

Run:
  ARIADNE_LIVE_CLOSED_LOOP=1 uv run pytest tests/test_closed_loop_live.py -q

Requires OpenAI-compatible credentials via env / workspace ``.env``
(``ARIADNE_BASE_URL``, ``ARIADNE_API_KEY``, ``ARIADNE_MODEL`` or load_settings).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ariadne.config import load_settings
from ariadne.kernel.turn import TurnApplication
from ariadne.memory.facade import Memory
from ariadne.model.openai_chat import OpenAIChatModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tasks import DeterministicVerifier, SQLiteTaskStore, TaskController
from ariadne.tools.registry import build_default_registry

_LIVE = os.environ.get("ARIADNE_LIVE_CLOSED_LOOP", "").strip() in {
    "1",
    "true",
    "yes",
    "on",
}


def _live_ready() -> tuple[bool, str]:
    if not _LIVE:
        return False, "set ARIADNE_LIVE_CLOSED_LOOP=1 to run live closed-loop"
    try:
        settings = load_settings(workspace=Path.cwd(), force_workspace=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"load_settings failed: {exc}"
    if not (settings.api_key or "").strip():
        return False, "no API key configured"
    if not (settings.base_url or "").strip():
        return False, "no base_url configured"
    return True, ""


_ready, _reason = _live_ready()
pytestmark = pytest.mark.skipif(not _ready, reason=_reason or "live closed-loop off")


def test_live_closed_loop_write_and_verify(tmp_path: Path) -> None:
    """Real model must plan + write marker under task mode with deterministic checks."""
    settings = load_settings(workspace=tmp_path / "ws", force_workspace=True)
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir()

    marker = "ariadne-live-closed-loop"
    prompt = (
        "In closed-loop task mode: create /workspace/live_marker.txt containing "
        f"exactly the line `{marker}` (and nothing else important). "
        "Use submit_task_plan first with path_exists + file_contains checks, "
        "then sandbox_write_file. Do not invent tool results."
    )

    memory = Memory.local(path=data / "memory")
    skills = SkillStore.from_dirs([], strict=False, user_root=data / "skills")
    tools = build_default_registry(
        memory=memory, skills=skills, enable_deferred_demo=False
    )
    controller = TaskController(
        store=SQLiteTaskStore(data / "tasks.sqlite3"),
        verifier=DeterministicVerifier(workspace),
    )
    model = OpenAIChatModel(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    app = TurnApplication(
        model=model,
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=data),
        task_controller=controller,
        guardrails_enabled=False,
        tool_loop_limit=16,
        task_mode_policy="auto",
        max_tokens=min(settings.max_tokens, 4096),
    )

    kinds: list[str] = []

    async def collect() -> object:
        result = None
        async for ev in app.run_events(
            prompt=prompt,
            session_id="live-closed-loop",
            metadata={"task_mode": True},
        ):
            kinds.append(ev.kind)
            if ev.kind in {"turn_completed", "turn_failed"}:
                result = ev.data.get("result")
        return result

    result = asyncio.run(collect())
    assert result is not None
    assert "task_mode_resolved" in kinds
    # Prefer full success; allow needs_input only if the model asked (rare)
    path = workspace / "live_marker.txt"
    if result.status == "completed":
        assert path.is_file(), "verifier completed but marker missing"
        assert marker in path.read_text(encoding="utf-8")
        assert "task_completed" in kinds
    else:
        # Soft fail with diagnostics so CI opt-in failures are debuggable
        body = path.read_text(encoding="utf-8") if path.is_file() else ""
        pytest.fail(
            f"live closed-loop status={result.status!r} error={result.error!r} "
            f"kinds={kinds} marker_exists={path.is_file()} body={body!r}"
        )
