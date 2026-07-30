from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.kernel.turn import TurnApplication
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore
from ariadne.memory.transcript import TranscriptStore
from ariadne.model.fake import FakeModel
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _tc(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def test_end_to_end_shell_memory_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo project\n", encoding="utf-8")
    data = tmp_path / "data"

    skills_dir = tmp_path / "skills"
    skill = skills_dir / "shell_project_notes"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        """---
name: shell_project_notes
description: Write NOTES.md for a project using shell tools.
keywords: [notes]
requires_tools: [sandbox_exec]
---

# Notes skill

Use sandbox_exec to inspect and write NOTES.md.
""",
        encoding="utf-8",
    )

    state_store = ConversationStateStore(data / "s.json")
    memory = MemoryFacade(
        transcript=TranscriptStore(data / "t.jsonl"),
        curated=CuratedStore(data / "c.json"),
        state=state_store,
        summaries=TurnSummaryStore(data / "sum.json"),
        semantic=SemanticIndex(data / "sem.json"),
        projection=ProjectionWorker(path=data / "projection_jobs.json", state_store=state_store),
    )
    skills = SkillStore.from_dir(skills_dir)
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=True)
    sandbox = LocalWorkdirSandbox(workspace=workspace, data_dir=data)

    step = {"n": 0}

    def script(messages: list[dict[str, Any]], model_tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        n = step["n"]
        step["n"] += 1
        tool_names = {
            (t.get("function") or {}).get("name")
            for t in (model_tools or [])
        }
        if n == 0:
            assert "sandbox_exec" in tool_names
            assert "echo_note" not in tool_names  # deferred
            return {
                "content": None,
                "tool_calls": [
                    _tc("search_skills", {"query": "notes"}, "c1"),
                    _tc("memory", {"action": "add", "content": "Prefer short bullets", "scope": "user"}, "c2"),
                ],
            }
        if n == 1:
            return {
                "content": None,
                "tool_calls": [_tc("load_skill", {"name": "shell_project_notes"}, "c3")],
            }
        if n == 2:
            return {
                "content": None,
                "tool_calls": [
                    _tc(
                        "sandbox_exec",
                        {"cmd": "printf '# Notes\\n\\n- demo project\\n' > NOTES.md && cat NOTES.md"},
                        "c4",
                    )
                ],
            }
        if n == 3:
            # conversation_state is named_deferred — load schema before apply.
            assert "conversation_state" not in tool_names
            return {
                "content": None,
                "tool_calls": [
                    _tc("tool_search", {"tool_names": ["conversation_state"]}, "c5"),
                ],
            }
        if n == 4:
            assert "conversation_state" in tool_names
            return {
                "content": None,
                "tool_calls": [
                    _tc(
                        "conversation_state",
                        {
                            "action": "apply",
                            "operations": [
                                {
                                    "op": "ensure_entity",
                                    "entity_id": "doc:notes",
                                    "type": "file",
                                    "evidence_quote": "demo project",
                                },
                                {
                                    "op": "set_attribute",
                                    "entity_id": "doc:notes",
                                    "key": "path",
                                    "value": "NOTES.md",
                                    "evidence_quote": "demo project",
                                },
                            ],
                        },
                        "c6",
                    )
                ],
            }
        return {"content": "Created NOTES.md and remembered preference for short bullets."}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        sandbox_backend=sandbox,
        memory=memory,
        skills=skills,
        tool_loop_limit=10,
        prefer_deferred_tools=True,
    )

    result = asyncio.run(app.run(prompt="Write project notes with the notes skill", session_id="sess1"))
    assert result.status == "completed", result.error
    assert (workspace / "NOTES.md").exists()
    assert "demo project" in (workspace / "NOTES.md").read_text()
    names = [c.name for c in result.tool_calls]
    assert "search_skills" in names
    assert "load_skill" in names
    assert "sandbox_exec" in names
    assert "memory" in names
    assert "conversation_state" in names
    snap, count = memory.curated.snapshot_text(session_id="sess1")
    assert count == 1
    assert "short bullets" in snap
    state_text, entities = memory.state.render("sess1")
    assert entities == 1
    assert "NOTES.md" in state_text
    kinds = {e.kind for e in result.skill_events}
    assert "search" in kinds or "load" in kinds or "index" in kinds
    assert memory.summaries.list_ready("sess1")
    hits = memory.semantic.search(session_id="sess1", query="notes project", limit=3)
    assert hits
    assert result.schema_metrics
    assert result.schema_metrics[0].schema_chars > 0
    assert result.schema_metrics[0].deferred_count >= 1
    jobs = memory.projection.list_jobs(session_id="sess1") if memory.projection else []
    assert jobs and jobs[-1]["status"] == "pending"


def test_stream_events_fake_model(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = MemoryFacade(
        transcript=TranscriptStore(data / "t.jsonl"),
        curated=CuratedStore(data / "c.json"),
        state=ConversationStateStore(data / "s.json"),
        summaries=TurnSummaryStore(data / "sum.json"),
        semantic=SemanticIndex(data / "sem.json"),
    )
    skills = SkillStore({})
    tools = build_default_registry(memory=memory, skills=skills)
    sandbox = LocalWorkdirSandbox(workspace=workspace, data_dir=data)

    def script(messages, tools_payload):
        return {"content": "hello streamed world"}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        sandbox_backend=sandbox,
        memory=memory,
        skills=skills,
        stream_model=True,
    )

    async def collect():
        events = []
        async for ev in app.run_events(prompt="hi", session_id="s1"):
            events.append(ev)
        return events

    events = asyncio.run(collect())
    kinds = [e.kind for e in events]
    assert "turn_started" in kinds
    assert "model_delta" in kinds
    assert "turn_completed" in kinds
    deltas = "".join(e.data.get("text", "") for e in events if e.kind == "model_delta")
    assert "hello" in deltas
    final = next(e.data["result"] for e in events if e.kind == "turn_completed")
    assert final.status == "completed"
    assert "streamed" in final.text
