"""Per-turn tool history persistence for web UI badges."""

from __future__ import annotations

from pathlib import Path

from ariadne.cli.sessions import annotate_turn_indices
from ariadne.cli.turn_history import (
    append_turn_from_result_payload,
    append_turn_record,
    get_turn_record,
    list_turn_records,
    turns_path,
)


def test_append_and_list_turns(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    r1 = append_turn_record(
        sd,
        "s1",
        turn_id="t-a",
        tools=[
            {
                "call_id": "c1",
                "name": "sandbox_read_file",
                "status": "completed",
                "arguments": {"path": "/workspace/a"},
                "output": "ok",
            }
        ],
        usage={"total_tokens": 12},
        text="done",
    )
    assert r1["turn_index"] == 1
    assert turns_path(sd, "s1").is_file()
    # duplicate last turn_id is no-op
    r1b = append_turn_record(sd, "s1", turn_id="t-a", tools=[])
    assert r1b["turn_id"] == "t-a"
    rows = list_turn_records(sd, "s1")
    assert len(rows) == 1
    assert rows[0]["tool_count"] == 1
    append_turn_record(sd, "s1", turn_id="t-b", tools=[])
    rows = list_turn_records(sd, "s1")
    assert [r["turn_index"] for r in rows] == [1, 2]
    got = get_turn_record(sd, "s1", "t-a")
    assert got is not None
    assert got["tools"][0]["name"] == "sandbox_read_file"


def test_from_result_payload(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    payload = {
        "turn_id": "xyz",
        "status": "completed",
        "text": "hello",
        "model": "LongCat-2.0",
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        "tool_calls": [
            {
                "call_id": "1",
                "name": "sandbox_list_dir",
                "status": "completed",
                "arguments": {"path": "/workspace"},
                "output": ["a"],
            }
        ],
    }
    row = append_turn_from_result_payload(sd, "web-1", payload)
    assert row is not None
    assert row["turn_index"] == 1
    assert row["tools"][0]["name"] == "sandbox_list_dir"


def test_annotate_turn_indices() -> None:
    msgs = annotate_turn_indices(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
    )
    assert [m["turn_index"] for m in msgs] == [1, 1, 2, 2]
