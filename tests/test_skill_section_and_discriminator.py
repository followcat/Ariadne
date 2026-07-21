"""Skill body section load + optional discriminator metadata (real SkillStore)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ariadne.memory import Memory
from ariadne.skills.store import SkillStore
from ariadne.tools.registry import build_default_registry


def _write_pack(
    root: Path,
    *,
    name: str,
    description: str,
    body: str,
    extra_fm: str = "",
) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
{extra_fm}---

{body}
""",
        encoding="utf-8",
    )


def test_body_section_extracts_named_heading(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        name="sect_demo",
        description="Section demo skill for load_skill section=.",
        body="""# Title

Preamble not in a section.

## Usage

Use the CLI carefully.

## Schema

Fields: a, b, c.

## Examples

Example one.
""",
        extra_fm="keywords: [section]\n",
    )
    store = SkillStore.from_dir(tmp_path)
    skill = store.get("sect_demo")
    assert skill is not None
    usage = skill.body_section("usage")
    assert "CLI carefully" in usage
    assert "Fields:" not in usage
    schema = skill.body_section("Schema")
    assert "Fields: a, b, c" in schema
    full = skill.body_section("full")
    assert "Preamble" in full and "Example one" in full
    assert skill.body_section("missing_section") == ""


def test_trigger_clues_boost_selection_over_peer(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        name="plot_chart",
        description="Generic chart plotting helper.",
        body="Draw charts.",
        extra_fm="keywords: [chart, plot]\n",
    )
    _write_pack(
        tmp_path,
        name="trend_line",
        description="A-share index trend line drawings.",
        body="Draw 走势图 with closing prices.",
        extra_fm=(
            "keywords: [chart, plot]\n"
            "trigger_clues: [走势图, trend line, a-share index]\n"
            "distinct_from: [plot_chart]\n"
            "key_difference: trend chart not generic bar chart\n"
        ),
    )
    store = SkillStore.from_dir(tmp_path)
    hits = store.search("帮我画走势图 a-share", limit=5)
    assert hits, "expected search hits"
    assert hits[0].name == "trend_line", [h.name for h in hits]
    # Query that names the other skill should demote trend_line somewhat
    hits2 = store.search_scored("plot_chart generic bars", limit=5)
    names = [s.name for _, s in hits2]
    assert "plot_chart" in names


def test_load_skill_tool_section_via_registry(tmp_path: Path) -> None:
    _write_pack(
        tmp_path,
        name="tool_sect",
        description="Loaded via registry load_skill with section.",
        body="""## Usage

Call me for usage only.

## Schema

secret_schema_token_xyz
""",
        extra_fm="keywords: [toolsect]\n",
    )
    store = SkillStore.from_dir(tmp_path)
    memory = Memory.local(path=tmp_path / "mem")
    reg = build_default_registry(memory=memory, skills=store, enable_deferred_demo=False)

    async def run() -> dict:
        from ariadne.tools.registry import ToolContext

        ctx = ToolContext(
            session_id="s1",
            turn_id="t1",
            sandbox=None,
            memory=memory,
            skills=store,
        )
        return await reg.invoke("load_skill", {"name": "tool_sect", "section": "usage"}, ctx)

    payload = asyncio.run(run())
    assert payload["section"] == "usage"
    assert "usage only" in payload["body"].lower() or "Call me for usage" in payload["body"]
    assert "secret_schema_token_xyz" not in payload["body"]
