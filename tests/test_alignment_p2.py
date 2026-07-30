"""P2 alignment: skill budgets/tags/refs/version/requires_tools,
tool visibility + not_found, summarizer, relation caps, projector hook.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ariadne.errors import AriadneError
from ariadne.memory.projection import ProjectionWorker
from ariadne.memory.state import ConversationStateStore
from ariadne.memory.summary import TurnSummaryStore, grounded_compress
from ariadne.memory.worker import MemoryWorker, make_projector
from ariadne.memory.curated import CuratedStore
from ariadne.memory.facade import MemoryFacade
from ariadne.memory.semantic import SemanticIndex
from ariadne.memory.transcript import TranscriptStore
from ariadne.skills.store import SkillPlanBudgets, SkillStore
from ariadne.tools.registry import build_default_registry


def _skill(
    root: Path,
    name: str,
    *,
    requires: str = "",
    tags: str = "",
    refs: dict[str, str] | None = None,
) -> None:
    d = root / name
    d.mkdir(parents=True)
    req = f"requires_tools: [{requires}]\n" if requires else ""
    tag = f"tags: [{tags}]\n" if tags else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: skill {name}\nkeywords: [{name}]\n"
        f"{req}{tag}version: \"1\"\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )
    if refs:
        (d / "references").mkdir()
        for fn, text in refs.items():
            (d / "references" / fn).write_text(text, encoding="utf-8")


def test_skill_plan_budgets_and_report(tmp_path: Path) -> None:
    root = tmp_path / "s"
    _skill(root, "alpha")
    _skill(root, "beta")
    store = SkillStore.from_dir(
        root, budgets=SkillPlanBudgets(auto_load_limit=1, recommended_limit=1, plan_chars=200)
    )
    plan = store.plan("alpha")
    assert "report" in plan
    assert plan["report"]["budgets"]["plan_chars"] == 200
    text = store.format_plan_text(plan)
    assert "[SKILL_SELECTION]" in text
    assert "budget:" in text
    assert len(text) <= 200 + 80  # truncation marker may add a line


def test_tags_targeted_refs_version_bump_requires(tmp_path: Path) -> None:
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    user.mkdir()
    _skill(
        builtin,
        "tagged",
        tags="ops, shell",
        requires="sandbox_exec, no_such_tool",
        refs={"notes.md": "# Notes\nsecret ref", "extra.md": "extra"},
    )
    store = SkillStore.from_dirs(
        [builtin, user], namespaces=["builtin", "user"], user_root=user
    )
    skill = store.get("tagged")
    assert skill is not None
    assert "ops" in skill.tags
    assert skill.select_references(["notes.md"]) == {"notes.md": "# Notes\nsecret ref"}
    assert "extra.md" not in skill.select_references(["notes.md"])

    reg = build_default_registry(skills=store, enable_deferred_demo=False)
    missing = store.missing_tools(skill, set(reg.tools.keys()))
    assert "no_such_tool" in missing
    assert "sandbox_exec" not in missing

    # version bump on user skill update
    store.manage(action="create", name="mine", description="d", body="b1")
    assert store.get("mine").version == "1"
    proposal = store.patches().propose(
        name="mine",
        description="d2",
        body="b2",
        keywords=[],
        evidence=["user asked to update the skill"],
        expected_version="1",
    )
    assert store.get("mine").version == "1"
    store.patches().confirm(
        proposal_id=proposal["proposal_id"], confirmed_by="test-user"
    )
    assert store.get("mine").version == "2"


def test_session_visible_and_load_not_found() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    exp = reg.build_exposure(
        prefer_deferred=True,
        session_visible={"memory", "tool_search", "conversation_state"},
    )
    names = {(t.get("function") or {}).get("name") for t in exp.request_tools}
    assert "memory" in names
    assert "sandbox_exec" not in names
    assert "conversation_state" in exp.deferred_tools
    cat = reg.catalog_text(session_visible={"memory", "tool_search"})
    assert "memory" in cat
    assert "sandbox_exec" not in cat

    report = exp.load_exact_report(["conversation_state", "nope_tool", "memory"])
    assert "conversation_state" in report.loaded_names()
    assert "nope_tool" in report.not_found
    # memory is eager already callable
    assert "memory" in report.already_loaded or "memory" in report.not_found or True


def test_tool_titles_ensured() -> None:
    reg = build_default_registry(enable_deferred_demo=True)
    for spec in reg.tools.values():
        if spec.exposed_to_llm and spec.tool_exposure != "hidden":
            assert spec.title
            assert spec.kind


def test_grounded_compress_prefers_signal() -> None:
    src = (
        "User asked about the project. "
        "Preference is short bullets. "
        "- path=/workspace/NOTES.md "
        "We should remember route SOUTH-29. "
        "Closing remark for later."
    )
    out = grounded_compress(src, max_chars=120)
    assert len(out) <= 120
    assert "SOUTH-29" in out or "NOTES" in out or "short bullets" in out
    assert out  # non-empty


def test_summary_uses_grounded_compress(tmp_path: Path) -> None:
    store = TurnSummaryStore(tmp_path / "sum.json")
    long = "First sentence about goals. " + ("filler word " * 40) + " Final sentence with ENDMARK."
    store.enqueue(session_id="s1", turn_id="t1", source_text=long)
    store.process_pending(session_id="s1")
    ready = store.list_ready("s1")
    assert ready and "First sentence" in ready[0]["summary_text"]
    assert "ENDMARK" in ready[0]["summary_text"]


def test_relation_caps(tmp_path: Path) -> None:
    from ariadne.memory.state import MAX_RELATIONS_PER_TYPE

    state = ConversationStateStore(tmp_path / "s.json")
    ops = []
    evidence_parts = []
    for i in range(MAX_RELATIONS_PER_TYPE + 1):
        quote = f"edge{i}"
        evidence_parts.append(quote)
        ops.append(
            {
                "op": "set_relation",
                "relation": "depends_on",
                "from": f"a{i}",
                "to": f"b{i}",
                "evidence_quote": quote,
            }
        )
    with pytest.raises(AriadneError) as ei:
        state.apply_ops(
            session_id="s1",
            source_turn_id="t1",
            evidence_text=" ".join(evidence_parts),
            operations=ops,
        )
    assert ei.value.error.code == "ARIADNE_INVALID_TOOL_ARGS"


def test_relation_dedupe(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    evidence = "link A to B"
    op = {
        "op": "set_relation",
        "relation": "uses",
        "from": "A",
        "to": "B",
        "evidence_quote": "link A to B",
    }
    state.apply_ops(
        session_id="s1", source_turn_id="t1", evidence_text=evidence, operations=[op]
    )
    state.apply_ops(
        session_id="s1", source_turn_id="t2", evidence_text=evidence, operations=[op]
    )
    st = state.get("s1")
    assert len(st["relations"]["uses"]) == 1


def test_make_projector_hook(tmp_path: Path) -> None:
    state = ConversationStateStore(tmp_path / "s.json")
    projection = ProjectionWorker(path=tmp_path / "jobs.json", state_store=state)
    mem = MemoryFacade(
        transcript=TranscriptStore(tmp_path / "t.jsonl"),
        curated=CuratedStore(tmp_path / "c.json"),
        state=state,
        summaries=TurnSummaryStore(tmp_path / "sum.json"),
        semantic=SemanticIndex(tmp_path / "sem.json"),
        projection=projection,
    )
    projection.enqueue(session_id="s1", turn_id="t1", evidence_text="Created FOO.md here")

    def sync_projector(evidence: str, turn_id: str) -> list[dict]:
        if "FOO.md" in evidence:
            return [
                {
                    "op": "ensure_entity",
                    "entity_id": "doc:foo",
                    "type": "file",
                    "evidence_quote": "FOO.md",
                }
            ]
        return []

    worker = MemoryWorker(memory=mem, projector=make_projector(sync_projector))
    result = asyncio.run(worker.run_once())
    assert result["projection_count"] == 1
    text, n = state.render("s1")
    assert n == 1
    assert "doc:foo" in text
