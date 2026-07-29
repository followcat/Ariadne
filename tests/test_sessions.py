"""Session listing / continue / parser coverage."""

from __future__ import annotations

import json
from pathlib import Path

from ariadne.cli.main import build_parser
from ariadne.cli.sessions import (
    delete_session,
    get_session_title,
    list_sessions,
    load_session_messages,
    most_recent,
    refresh_session_title,
    set_session_title,
    suggest_title_from_messages,
)


def _write_session(path: Path, users: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(users):
            fh.write(json.dumps({"role": "user", "content": f"q{i}", "turn_id": f"t{i}"}) + "\n")
            fh.write(json.dumps({"role": "assistant", "content": f"a{i}", "turn_id": f"t{i}"}) + "\n")


def test_list_sessions_counts_user_turns(tmp_path: Path) -> None:
    _write_session(tmp_path / "sessions" / "alpha.jsonl", 3)
    _write_session(tmp_path / "sessions" / "beta.jsonl", 1)
    sessions = list_sessions(tmp_path)
    assert {s.session_id for s in sessions} == {"alpha", "beta"}
    by_id = {s.session_id: s for s in sessions}
    assert by_id["alpha"].turns == 3
    assert by_id["beta"].turns == 1
    assert by_id["alpha"].preview.startswith("q0")
    assert most_recent(tmp_path) in {"alpha", "beta"}
    msgs = load_session_messages(tmp_path, "alpha", limit=10)
    assert msgs[0]["role"] == "user" and msgs[0]["content"] == "q0"
    assert msgs[0].get("turn_id") == "t0"
    assert msgs[-1]["role"] == "assistant" and msgs[-1]["content"] == "a2"
    assert msgs[-1].get("turn_id") == "t2"
    assert delete_session(tmp_path, "beta") is True
    assert delete_session(tmp_path, "beta") is False


def test_list_sessions_empty(tmp_path: Path) -> None:
    assert list_sessions(tmp_path) == []
    assert most_recent(tmp_path) is None


def test_suggest_and_refresh_title(tmp_path: Path) -> None:
    suggested = suggest_title_from_messages(
        [{"role": "user", "content": "帮我写一个部署脚本"}]
    )
    assert "部署" in suggested
    assert len(suggested) <= 40
    _write_session(tmp_path / "sessions" / "t1.jsonl", 1)
    # first user content is q0
    meta = refresh_session_title(tmp_path, "t1", force=True)
    assert meta is not None and meta["title"]
    assert meta["source"] == "auto"
    set_session_title(tmp_path, "t1", "我的主题", source="user")
    t, src = get_session_title(tmp_path, "t1")
    assert t == "我的主题" and src == "user"
    # auto refresh should not overwrite user title
    skipped = refresh_session_title(tmp_path, "t1", force=False)
    assert skipped and skipped.get("skipped") is True
    sessions = list_sessions(tmp_path)
    by_id = {s.session_id: s for s in sessions}
    assert by_id["t1"].title == "我的主题"
    assert by_id["t1"].title_source == "user"


def test_parser_continue_and_sessions_command() -> None:
    parser = build_parser()
    args = parser.parse_args(["-c", "chat"])
    assert args.continue_last is True
    args = parser.parse_args(["chat", "--continue"])
    assert args.continue_last is True
    args = parser.parse_args(["sessions"])
    assert args.command == "sessions"
    args = parser.parse_args(["run", "--approval-mode", "readonly", "hi"])
    assert args.approval_mode == "readonly"
    args = parser.parse_args(["chat", "--no-stream"])
    assert args.no_stream is True
