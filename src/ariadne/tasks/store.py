from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import AriadneError, app_error
from .models import TaskState


@dataclass(slots=True)
class SQLiteTaskStore:
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def _initialize(self) -> None:
        with self._connect() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    previous_digest TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(task_id, revision)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS active_tasks (
                    session_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE
                )
                """
            )
            con.execute("PRAGMA user_version = 1")

    def save(self, state: TaskState, *, expected_revision: int | None = None) -> TaskState:
        if state.goal != state.original_user_goal:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_GOAL_IMMUTABLE",
                    "task goal must remain identical to the original user goal",
                    task_id=state.task_id,
                )
            )
        expected = state.revision if expected_revision is None else int(expected_revision)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revision,payload FROM tasks WHERE task_id = ?", (state.task_id,)
            ).fetchone()
            current = int(row["revision"]) if row is not None else 0
            if current != expected:
                raise AriadneError(
                    app_error(
                        "ARIADNE_TASK_CONFLICT",
                        "task state was updated by another writer",
                        task_id=state.task_id,
                        expected_revision=expected,
                        actual_revision=current,
                    )
                )
            if row is not None:
                stored_payload = json.loads(str(row["payload"]))
                stored_original_goal = str(stored_payload.get("original_user_goal") or "")
                if stored_original_goal != state.original_user_goal:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_TASK_GOAL_IMMUTABLE",
                            "original user goal cannot be changed after task creation",
                            task_id=state.task_id,
                        )
                    )
            now = time.time()
            state.revision = current + 1
            state.updated_at = now
            payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
            payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            previous_event = con.execute(
                """
                SELECT event_digest FROM task_events
                WHERE task_id=? ORDER BY revision DESC LIMIT 1
                """,
                (state.task_id,),
            ).fetchone()
            previous_digest = (
                str(previous_event["event_digest"]) if previous_event is not None else ""
            )
            event_digest = hashlib.sha256(
                (
                    f"{state.task_id}\0{state.revision}\0{previous_digest}\0"
                    f"{payload_digest}"
                ).encode("utf-8")
            ).hexdigest()
            con.execute(
                """
                INSERT INTO tasks(task_id, session_id, status, revision, payload, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                  session_id=excluded.session_id,
                  status=excluded.status,
                  revision=excluded.revision,
                  payload=excluded.payload,
                  updated_at=excluded.updated_at
                """,
                (
                    state.task_id,
                    state.session_id,
                    state.status,
                    state.revision,
                    payload,
                    state.created_at,
                    state.updated_at,
                ),
            )
            if state.status in {"active", "needs_input"}:
                other = con.execute(
                    "SELECT task_id FROM active_tasks WHERE session_id = ?",
                    (state.session_id,),
                ).fetchone()
                if other is not None and str(other["task_id"]) != state.task_id:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_TASK_ACTIVE_EXISTS",
                            "session already has an active task",
                            session_id=state.session_id,
                            active_task_id=str(other["task_id"]),
                        )
                    )
                con.execute(
                    """
                    INSERT INTO active_tasks(session_id, task_id) VALUES(?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET task_id=excluded.task_id
                    """,
                    (state.session_id, state.task_id),
                )
            else:
                con.execute(
                    "DELETE FROM active_tasks WHERE session_id = ? AND task_id = ?",
                    (state.session_id, state.task_id),
                )
            con.execute(
                """
                INSERT INTO task_events(
                    task_id,revision,payload,payload_digest,previous_digest,
                    event_digest,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    state.task_id,
                    state.revision,
                    payload,
                    payload_digest,
                    previous_digest,
                    event_digest,
                    now,
                ),
            )
        return state

    @staticmethod
    def _decode_payload(raw: str) -> TaskState:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AriadneError(
                app_error(
                    "ARIADNE_TASK_STORE_CORRUPT",
                    f"stored task payload is invalid JSON: {exc}",
                )
            ) from exc
        if not isinstance(value, dict):
            raise AriadneError(
                app_error("ARIADNE_TASK_STORE_CORRUPT", "stored task payload is not an object")
            )
        return TaskState.from_dict(value)

    def load(self, task_id: str) -> TaskState | None:
        with self._connect() as con:
            row = con.execute("SELECT payload FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return self._decode_payload(str(row["payload"]))

    def load_active(self, session_id: str) -> TaskState | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT t.payload FROM active_tasks a
                JOIN tasks t ON t.task_id = a.task_id
                WHERE a.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_payload(str(row["payload"]))

    def list_for_session(self, session_id: str) -> list[TaskState]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT payload FROM tasks WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [self._decode_payload(str(row["payload"])) for row in rows]

    def load_revision(self, task_id: str, revision: int) -> TaskState | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND revision=?",
                (task_id, int(revision)),
            ).fetchone()
        if row is None:
            return None
        return self._decode_payload(str(row["payload"]))

    def event_history(self, task_id: str) -> list[dict[str, str | int | float]]:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT revision,payload_digest,previous_digest,event_digest,created_at
                FROM task_events WHERE task_id=? ORDER BY revision
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "revision": int(row["revision"]),
                "payload_digest": str(row["payload_digest"]),
                "previous_digest": str(row["previous_digest"]),
                "event_digest": str(row["event_digest"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def verify_event_history(self, task_id: str) -> bool:
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT revision,payload,payload_digest,previous_digest,event_digest
                FROM task_events WHERE task_id=? ORDER BY revision
                """,
                (task_id,),
            ).fetchall()
        previous_digest = ""
        for row in rows:
            payload = str(row["payload"])
            payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if payload_digest != str(row["payload_digest"]):
                return False
            if str(row["previous_digest"]) != previous_digest:
                return False
            expected_event_digest = hashlib.sha256(
                (
                    f"{task_id}\0{int(row['revision'])}\0{previous_digest}\0"
                    f"{payload_digest}"
                ).encode("utf-8")
            ).hexdigest()
            if expected_event_digest != str(row["event_digest"]):
                return False
            previous_digest = expected_event_digest
        return bool(rows)
