from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ariadne.kernel.turn import TurnApplication
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
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

    memory = MemoryFacade(
        transcript=TranscriptStore(data / "t.jsonl"),
        curated=CuratedStore(data / "c.json"),
        state=ConversationStateStore(data / "s.json"),
        summaries=TurnSummaryStore(data / "sum.json"),
        semantic=SemanticIndex(data / "sem.json"),
    )
    skills = SkillStore.from_dir(skills_dir)
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=True)
    sandbox = LocalWorkdirSandbox(workspace=workspace, data_dir=data)

    step = {"n": 0}

    def script(messages: list[dict[str, Any]], model_tools: list[dict[str, Any]] | None) -> dict[str, Any]:
        n = step["n"]
        step["n"] += 1
        # Ensure deferred echo_note is not in initial tools list
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
                                    "evidence_quote": "NOTES.md",
                                },
                                {
                                    "op": "set_attribute",
                                    "entity_id": "doc:notes",
                                    "key": "path",
                                    "value": "NOTES.md",
                                    "evidence_quote": "NOTES.md",
                                },
                            ],
                            "evidence_text": "Created NOTES.md in project",
                        },
                        "c5",
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
    # durable memory persisted
    snap, count = memory.curated.snapshot_text(session_id="sess1")
    assert count == 1
    assert "short bullets" in snap
    state_text, entities = memory.state.render("sess1")
    assert entities == 1
    assert "NOTES.md" in state_text
    # skill events recorded
    kinds = {e.kind for e in result.skill_events}
    assert "search" in kinds or "load" in kinds or "index" in kinds
    # L1/L4 written
    assert memory.summaries.list_ready("sess1")
    hits = memory.semantic.search(session_id="sess1", query="notes project", limit=3)
    assert hits
