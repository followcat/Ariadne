"""P3: skill budget settings, native tool search, LLM summarizer, OOP worker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ariadne.config import Settings, load_settings
from ariadne.errors import AriadneError
from ariadne.memory.llm_summary import make_llm_compressor
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore, grounded_compress
from ariadne.memory.worker import spawn_worker_process
from ariadne.model.fake import FakeModel
from ariadne.tools.registry import ToolContext, build_default_registry


def test_settings_skill_budgets_and_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIADNE_SKILL_AUTO_LOAD_LIMIT", raising=False)
    monkeypatch.setenv("ARIADNE_SKILL_AUTO_LOAD_LIMIT", "3")
    monkeypatch.setenv("ARIADNE_TOOL_SEARCH_MODE", "native")
    monkeypatch.setenv("ARIADNE_SUMMARY_MODE", "llm")
    settings = load_settings(workspace=tmp_path, force_workspace=True)
    assert settings.skill_auto_load_limit == 3
    assert settings.tool_search_mode == "native"
    assert settings.summary_mode == "llm"
    b = settings.skill_plan_budgets()
    assert b.auto_load_limit == 3
    assert b.recommended_limit == 5


def test_settings_cli_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIADNE_SKILL_PLAN_CHARS", "500")
    settings = load_settings(
        workspace=tmp_path,
        force_workspace=True,
        skill_plan_chars=900,
        tool_search_mode="none",
        summary_mode="grounded",
    )
    assert settings.skill_plan_chars == 900
    assert settings.tool_search_mode == "none"


def test_invalid_modes_fastfail(tmp_path: Path) -> None:
    with pytest.raises(AriadneError) as ei:
        load_settings(workspace=tmp_path, force_workspace=True, tool_search_mode="magic")
    assert ei.value.error.code == "ARIADNE_CONFIG_INVALID"
    with pytest.raises(AriadneError):
        load_settings(workspace=tmp_path, force_workspace=True, summary_mode="poetry")


def test_native_search_mode_auto_materializes() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=True, client_search_mode="native")
    names = {(t.get("function") or {}).get("name") for t in exp.request_tools}
    assert "tool_search" not in names
    assert "conversation_state" in exp.deferred_tools
    assert "conversation_state" not in exp.callable_function_names
    assert exp.client_search_mode == "native"
    assert exp.ensure_callable("conversation_state") is True
    assert "conversation_state" in exp.callable_function_names


def test_native_invoke_without_tool_search() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=True, client_search_mode="native")

    async def go() -> Any:
        ctx = ToolContext(
            session_id="s",
            turn_id="t",
            sandbox=None,
            exposure=exp,
        )
        # echo_note is deferred; native mode should auto-load then run.
        return await reg.invoke("echo_note", {"note": "hi"}, ctx)

    out = asyncio.run(go())
    assert out == {"note": "hi"}
    assert "echo_note" in exp.callable_function_names


def test_none_search_mode_hides_deferred() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(prefer_deferred=True, client_search_mode="none")
    assert not exp.deferred_tools
    assert "tool_search" not in {
        (t.get("function") or {}).get("name") for t in exp.request_tools
    }


def test_llm_compressor_uses_model_then_fallback(tmp_path: Path) -> None:
    def script(messages, tools):
        return {"content": "User preferred short bullets. Path NOTES.md."}

    model = FakeModel(script=script)
    compress = make_llm_compressor(model, max_chars=80, fallback=True)
    src = "First long preamble. Preference is short bullets. Path NOTES.md. Trailing noise " * 5
    out = compress(src)
    assert "short bullets" in out or "NOTES" in out
    assert len(out) <= 80

    def boom(messages, tools):
        raise RuntimeError("model down")

    compress2 = make_llm_compressor(FakeModel(script=boom), max_chars=60, fallback=True)
    out2 = compress2(src)
    assert out2  # grounded fallback
    assert len(out2) <= 60


def test_spawn_worker_process(tmp_path: Path) -> None:
    data = tmp_path / "data"
    mem = data / "memory"
    mem.mkdir(parents=True)
    state = ConversationStateStore(path=mem / "state.json")
    proj = ProjectionWorker(path=mem / "projection_jobs.json", state_store=state)
    sums = TurnSummaryStore(path=mem / "summaries.json")
    sums.enqueue(session_id="s1", turn_id="t1", source_text="hello worker world")
    proj.enqueue(session_id="s1", turn_id="t1", evidence_text="noop")

    proc = spawn_worker_process(data_dir=data, once=True)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, (out, err)
    assert "summaries=" in out
    assert sums.pending_count("s1") == 0
    assert proj.pending_lag("s1") == 0


def test_parser_p3_flags() -> None:
    from ariadne.cli.main import build_parser

    p = build_parser()
    args = p.parse_args(
        [
            "--skill-auto-load",
            "2",
            "--skill-recommended",
            "4",
            "--tool-search-mode",
            "native",
            "--summary-mode",
            "llm",
            "doctor",
        ]
    )
    assert args.skill_auto_load_limit == 2
    assert args.skill_recommended_limit == 4
    assert args.tool_search_mode == "native"
    assert args.summary_mode == "llm"
