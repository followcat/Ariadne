"""Session listing / continue / parser coverage."""

from __future__ import annotations

import json
from pathlib import Path

from ariadne.cli.main import build_parser
from ariadne.cli.sessions import list_sessions, most_recent


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
    assert most_recent(tmp_path) in {"alpha", "beta"}


def test_list_sessions_empty(tmp_path: Path) -> None:
    assert list_sessions(tmp_path) == []
    assert most_recent(tmp_path) is None


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
