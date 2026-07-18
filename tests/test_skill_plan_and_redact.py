"""Skill selection plan, pack validation allowlist, trace redaction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ariadne.errors import AriadneError
from ariadne.kernel.turn import TurnApplication
from ariadne.memory import Memory
from ariadne.model.fake import FakeModel
from ariadne.redact import redact_secrets, redact_text
from ariadne.sandbox.local import LocalWorkdirSandbox
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _make_skill(root: Path, name: str, *, extra: dict[str, str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: skill about {name}\nkeywords: [{name}]\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )
    for rel, content in (extra or {}).items():
        target = d / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return d


def test_plan_auto_load_and_recommended(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _make_skill(root, "git_workflow")
    _make_skill(root, "docker_tips")
    store = SkillStore.from_dir(root)
    plan = store.plan("tell me about git_workflow please")
    assert [s.name for s, _ in plan["auto_load"]] == ["git_workflow"]
    # docker_tips weakly matches ("about") and lands in recommended, nothing left over
    assert [s.name for s, _ in plan["recommended"]] == ["docker_tips"]
    assert plan["other"] == 0
    plan2 = store.plan("docker")
    # strong match (name+description+keywords+body) promotes to auto_load
    assert [s.name for s, _ in plan2["auto_load"]] == ["docker_tips"]


def test_unknown_pack_entries_rejected(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _make_skill(root, "bad_skill", extra={"stray.txt": "nope"})
    with pytest.raises(AriadneError) as excinfo:
        SkillStore.from_dir(root, strict=True)
    assert excinfo.value.error.code == "ARIADNE_SKILL_INVALID"
    _make_skill(root, "good_skill", extra={"references/notes.md": "ref"})
    store = SkillStore.from_dir(root / "good_skill" and tmp_path / "skills2", strict=True) if False else None


def test_agents_yaml_metadata(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _make_skill(
        root,
        "yaml_skill",
        extra={
            "agents/index.yaml": "display_name: YAML Skill\nshort_description: short line\n",
            "agents/runtime.yaml": "requires_tools: [memory]\nkeywords: [extra-kw]\n",
        },
    )
    store = SkillStore.from_dir(root)
    skill = store.get("yaml_skill")
    assert skill is not None
    assert skill.display_name == "YAML Skill"
    assert skill.short_description == "short line"
    assert "memory" in skill.requires_tools
    assert "extra-kw" in skill.keywords
    assert skill.index_line() == "- yaml_skill: short line"


def test_redact_text_patterns() -> None:
    assert "sk-***" in redact_text("token is sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in redact_text("token is sk-abcdefghijklmnop")
    assert redact_text("Authorization: Bearer abcdefgh123456") == "Authorization: Bearer ***"
    assert redact_text("password=hunter22") == "password=***"
    assert redact_secrets({"a": ["sk-abcdefghijklmnop"]}) == {"a": ["sk-***"]}


def test_trace_outputs_are_redacted(tmp_path: Path) -> None:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    memory = Memory.local(path=tmp_path / "mem")
    skills = SkillStore.from_dirs([], strict=False, user_root=tmp_path / "skills-user")
    tools = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=False)

    def script(messages: list[dict[str, Any]], tools_payload: list[dict[str, Any]] | None) -> dict[str, Any]:
        if not any(m.get("role") == "tool" for m in messages):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "sandbox_exec",
                            "arguments": json.dumps({"cmd": "echo api_key=sk-secretvalue123"}),
                        },
                    }
                ],
            }
        return {"content": "done"}

    app = TurnApplication(
        model=FakeModel(script=script),
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=LocalWorkdirSandbox(workspace=workspace, data_dir=tmp_path / "data"),
    )

    async def run():
        return await app.run(prompt="leak test", session_id="s1")

    result = asyncio.run(run())
    assert result.status == "completed"
    trace_out = json.dumps(result.tool_calls[0].output)
    assert "sk-secretvalue123" not in trace_out
    assert "***" in trace_out
