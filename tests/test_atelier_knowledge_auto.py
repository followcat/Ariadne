"""Main post-turn constrained 便签 update (shipped runner + knowledge)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ariadne.atelier.knowledge import (
    extract_knowledge_heuristic,
    filter_knowledge_update,
    knowledge_template,
    read_knowledge,
)
from ariadne.atelier.manager import AtelierManager
from ariadne.atelier.models import SessionType
from ariadne.atelier.runner import (
    maybe_update_knowledge_after_turn,
    update_knowledge_after_turn,
)


def test_heuristic_picks_agreement_not_chitchat() -> None:
    yes = extract_knowledge_heuristic(
        [{"role": "user", "content": "我们决定使用 JWT 做认证，旁支也按这个来"}]
    )
    assert yes.has_update
    assert any("JWT" in (u.new_text or "") for u in yes.updates)

    no = extract_knowledge_heuristic(
        [{"role": "user", "content": "哈哈好的谢谢"}]
    )
    assert not no.has_update

    q = extract_knowledge_heuristic(
        [{"role": "user", "content": "我们要不要用 Redis 啊？"}]
    )
    # question mark → skip
    assert not q.has_update


def test_filter_dedupes_existing_bullets() -> None:
    current = knowledge_template("demo") + "\n- 我们决定使用 JWT 做认证\n"
    raw = extract_knowledge_heuristic(
        [{"role": "user", "content": "我们决定使用 JWT 做认证"}]
    )
    filt = filter_knowledge_update(current, raw)
    assert not filt.has_update


def test_main_turn_writes_brief_branch_skips(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("auto-k", no_scan=True)
    main = mgr.get_or_create_main_session(proj.id)
    br = mgr.create_branch(proj.id, "exp")

    before = read_knowledge(proj)
    # noise on main → no write
    assert (
        maybe_update_knowledge_after_turn(
            proj,
            main,
            user_text="好的",
            assistant_text="嗯嗯",
        )
        is False
    )
    assert read_knowledge(proj) == before

    # agreement on main → write
    ok = maybe_update_knowledge_after_turn(
        proj,
        main,
        user_text="我们决定采用蜡笔粒子风格，纯本地运行",
        assistant_text="好的，已记下：蜡笔粒子 + 纯本地。",
    )
    assert ok is True
    after = read_knowledge(proj)
    assert after != before
    assert "蜡笔" in after or "本地" in after

    # branch never writes even with agreement language
    mid = read_knowledge(proj)
    assert (
        maybe_update_knowledge_after_turn(
            proj,
            br,
            user_text="我们决定改用油画风格",
            assistant_text="旁支里改了。",
        )
        is False
    )
    assert read_knowledge(proj) == mid
    assert br.type == SessionType.BRANCH


def test_async_update_knowledge_main_and_branch(tmp_path: Path) -> None:
    mgr = AtelierManager(root=tmp_path / "ateliers")
    proj = mgr.create_project("async-k", no_scan=True)
    main = mgr.get_or_create_main_session(proj.id)
    br = mgr.create_branch(proj.id, "side")

    async def run() -> None:
        r1 = await update_knowledge_after_turn(
            proj,
            main,
            user_text="随便聊聊天气",
            assistant_text="今天不错",
        )
        assert r1["updated"] is False
        assert r1["reason"] in {"no_update", "noop"}

        r2 = await update_knowledge_after_turn(
            proj,
            main,
            user_text="我们决定使用 ruff 做 lint",
            assistant_text="收到，统一用 ruff。",
        )
        assert r2["updated"] is True
        assert r2.get("ops")
        assert "ruff" in read_knowledge(proj).lower()

        snap = read_knowledge(proj)
        r3 = await update_knowledge_after_turn(
            proj,
            br,
            user_text="我们决定删除 ruff",
            assistant_text="旁支不要写便签",
        )
        assert r3["updated"] is False
        assert r3["reason"] == "branch_skip"
        assert read_knowledge(proj) == snap

    asyncio.run(run())
