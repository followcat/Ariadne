from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .models import Check
from .verify import DeterministicVerifier

SCHEDULE_CHECK_KINDS = {"path_exists", "path_absent", "file_contains", "image_file"}


@dataclass(slots=True)
class ScheduledGoalStore:
    """Host-cron queue for explicit, deterministic proactive goal checks."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_goals (
                    schedule_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    check_json TEXT NOT NULL,
                    interval_seconds REAL NOT NULL,
                    next_run_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0,
                    lease_token INTEGER NOT NULL DEFAULT 0,
                    last_result_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(scheduled_goals)").fetchall()
            }
            if "lease_token" not in columns:
                con.execute(
                    "ALTER TABLE scheduled_goals "
                    "ADD COLUMN lease_token INTEGER NOT NULL DEFAULT 0"
                )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS goal_notifications (
                    notification_id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    read_at REAL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schedule_id": str(row["schedule_id"]),
            "user_id": str(row["user_id"]),
            "session_id": str(row["session_id"]),
            "goal": str(row["goal"]),
            "check": json.loads(str(row["check_json"])),
            "interval_seconds": float(row["interval_seconds"]),
            "next_run_at": float(row["next_run_at"]),
            "status": str(row["status"]),
            "revision": int(row["revision"]),
            "lease_owner": str(row["lease_owner"]),
            "lease_until": float(row["lease_until"]),
            "lease_token": int(row["lease_token"]),
            "last_result": json.loads(str(row["last_result_json"])),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

    def create(
        self,
        *,
        user_id: str,
        session_id: str,
        goal: str,
        check: dict[str, Any],
        interval_seconds: float,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        if not user_id.strip() or not session_id.strip() or not goal.strip():
            raise AriadneError(
                app_error(
                    "ARIADNE_SCHEDULE_INVALID",
                    "user_id, session_id, and goal are required",
                )
            )
        if not 60 <= float(interval_seconds) <= 31_536_000:
            raise AriadneError(
                app_error(
                    "ARIADNE_SCHEDULE_INVALID",
                    "interval_seconds must be between 60 seconds and one year",
                )
            )
        parsed = Check.from_plan(check)
        if parsed.kind not in SCHEDULE_CHECK_KINDS:
            raise AriadneError(
                app_error(
                    "ARIADNE_SCHEDULE_INVALID",
                    "scheduled checks must be deterministic, read-only environment checks",
                    kind=parsed.kind,
                    allowed=sorted(SCHEDULE_CHECK_KINDS),
                )
            )
        schedule_id = f"schedule_{uuid.uuid4().hex[:12]}"
        now = time.time()
        run_at = now if next_run_at is None else float(next_run_at)
        encoded_check = json.dumps(asdict(parsed), ensure_ascii=False, separators=(",", ":"))
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO scheduled_goals(
                    schedule_id,user_id,session_id,goal,check_json,interval_seconds,
                    next_run_at,status,revision,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,'active',1,?,?)
                """,
                (
                    schedule_id,
                    user_id,
                    session_id,
                    goal.strip(),
                    encoded_check,
                    float(interval_seconds),
                    run_at,
                    now,
                    now,
                ),
            )
        return self.get(schedule_id, user_id=user_id)

    def get(self, schedule_id: str, *, user_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM scheduled_goals WHERE schedule_id=? AND user_id=?",
                (schedule_id, user_id),
            ).fetchone()
        if row is None:
            raise AriadneError(
                app_error("ARIADNE_SCHEDULE_NOT_FOUND", "scheduled goal not found")
            )
        return self._decode(row)

    def list(self, *, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT * FROM scheduled_goals WHERE user_id=? ORDER BY created_at",
                (user_id,),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def set_status(
        self,
        *,
        schedule_id: str,
        user_id: str,
        status: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        if status not in {"active", "paused", "cancelled"}:
            raise AriadneError(
                app_error(
                    "ARIADNE_SCHEDULE_INVALID",
                    "status must be active, paused, or cancelled",
                )
            )
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT revision FROM scheduled_goals WHERE schedule_id=? AND user_id=?",
                (schedule_id, user_id),
            ).fetchone()
            if row is None:
                raise AriadneError(
                    app_error("ARIADNE_SCHEDULE_NOT_FOUND", "scheduled goal not found")
                )
            current = int(row["revision"])
            if current != expected_revision:
                raise AriadneError(
                    app_error(
                        "ARIADNE_SCHEDULE_CONFLICT",
                        "scheduled goal revision conflict",
                        schedule_id=schedule_id,
                        expected_revision=expected_revision,
                        current_revision=current,
                    )
                )
            con.execute(
                """
                UPDATE scheduled_goals SET status=?, revision=revision+1,
                  lease_owner='', lease_until=0, updated_at=?
                WHERE schedule_id=? AND user_id=?
                """,
                (status, time.time(), schedule_id, user_id),
            )
        return self.get(schedule_id, user_id=user_id)

    def claim_due(
        self,
        *,
        user_id: str,
        worker_id: str,
        now: float | None = None,
        lease_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        clock = time.time() if now is None else float(now)
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT * FROM scheduled_goals
                WHERE user_id=? AND status='active' AND next_run_at<=?
                  AND (lease_owner='' OR lease_until<=?)
                ORDER BY next_run_at, created_at LIMIT 1
                """,
                (user_id, clock, clock),
            ).fetchone()
            if row is None:
                return None
            con.execute(
                """
                UPDATE scheduled_goals
                SET lease_owner=?, lease_until=?, lease_token=lease_token+1,
                  revision=revision+1, updated_at=?
                WHERE schedule_id=? AND revision=?
                """,
                (
                    worker_id,
                    clock + lease_seconds,
                    clock,
                    row["schedule_id"],
                    row["revision"],
                ),
            )
            claimed = con.execute(
                "SELECT * FROM scheduled_goals WHERE schedule_id=?",
                (row["schedule_id"],),
            ).fetchone()
        return self._decode(claimed)

    def run_due(
        self,
        *,
        user_id: str,
        worker_id: str,
        verifier: DeterministicVerifier,
        now: float | None = None,
        max_checks: int = 20,
    ) -> list[dict[str, Any]]:
        clock = time.time() if now is None else float(now)
        results: list[dict[str, Any]] = []
        for _ in range(max_checks):
            claimed = self.claim_due(
                user_id=user_id,
                worker_id=worker_id,
                now=clock,
            )
            if claimed is None:
                break
            check = Check.from_dict(claimed["check"])
            result = verifier.run(
                check,
                traces=[],
                attempt_id=f"{claimed['schedule_id']}:{claimed['revision']}",
            )
            result_dict = result.to_dict()
            if result.status == "pass":
                status = "completed"
                next_run = claimed["next_run_at"]
                kind = "goal_satisfied"
                message = f"Scheduled goal satisfied: {claimed['goal']}"
            elif result.status in {"fail", "not_run"}:
                status = "active"
                next_run = clock + float(claimed["interval_seconds"])
                kind = "goal_pending"
                message = f"Scheduled goal is still pending: {claimed['goal']}"
            else:
                status = "paused"
                next_run = claimed["next_run_at"]
                kind = "goal_check_error"
                message = f"Scheduled goal check paused after verifier error: {claimed['goal']}"
            notification_id = f"notification_{uuid.uuid4().hex[:12]}"
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                changed = con.execute(
                    """
                    UPDATE scheduled_goals SET
                      status=?, next_run_at=?, lease_owner='', lease_until=0,
                      last_result_json=?, revision=revision+1, updated_at=?
                    WHERE schedule_id=? AND lease_owner=?
                      AND user_id=? AND lease_token=? AND revision=? AND lease_until>?
                    """,
                    (
                        status,
                        next_run,
                        json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                        clock,
                        claimed["schedule_id"],
                        worker_id,
                        user_id,
                        claimed["lease_token"],
                        claimed["revision"],
                        clock,
                    ),
                ).rowcount
                if changed != 1:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_SCHEDULE_CONFLICT",
                            "scheduled goal lease was lost before completion",
                            schedule_id=claimed["schedule_id"],
                            lease_token=claimed["lease_token"],
                        )
                    )
                con.execute(
                    """
                    INSERT INTO goal_notifications(
                      notification_id,schedule_id,user_id,kind,message,result_json,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        notification_id,
                        claimed["schedule_id"],
                        user_id,
                        kind,
                        message,
                        json.dumps(result_dict, ensure_ascii=False, separators=(",", ":")),
                        clock,
                    ),
                )
            results.append(
                {
                    "schedule_id": claimed["schedule_id"],
                    "status": status,
                    "notification_id": notification_id,
                    "notification_kind": kind,
                    "result": result_dict,
                }
            )
        return results

    def notifications(
        self, *, user_id: str, unread_only: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM goal_notifications WHERE user_id=?"
        args: list[Any] = [user_id]
        if unread_only:
            query += " AND read_at IS NULL"
        query += " ORDER BY created_at"
        with self._connect() as con:
            rows = con.execute(query, args).fetchall()
        return [
            {
                "notification_id": str(row["notification_id"]),
                "schedule_id": str(row["schedule_id"]),
                "kind": str(row["kind"]),
                "message": str(row["message"]),
                "result": json.loads(str(row["result_json"])),
                "created_at": float(row["created_at"]),
                "read_at": float(row["read_at"]) if row["read_at"] is not None else None,
            }
            for row in rows
        ]
