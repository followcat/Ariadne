from __future__ import annotations

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
                CREATE TABLE IF NOT EXISTS active_tasks (
                    session_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE
                )
                """
            )
            con.execute("PRAGMA user_version = 1")

    def save(self, state: TaskState, *, expected_revision: int | None = None) -> TaskState:
        expected = state.revision if expected_revision is None else int(expected_revision)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revision FROM tasks WHERE task_id = ?", (state.task_id,)
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
            now = time.time()
            state.revision = current + 1
            state.updated_at = now
            payload = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
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
