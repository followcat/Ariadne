"""SQLite persistence for conversation-state documents and projections."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..errors import AriadneError, app_error
from .json_file import locked_read_json
from .state_schema import FTS_SQL, SCHEMA_SQL
from .working_set import lexical_terms

SCHEMA_VERSION = "2"


def canonical_state_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def evidence_hash(operation: dict[str, Any]) -> str:
    payload = {
        "authority": str(operation.get("authority") or ""),
        "evidence_quote": str(operation.get("evidence_quote") or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def resolve_db_path(identity_path: Path) -> Path:
    """Map the identity path (often ``state.json``) to the SQLite file."""

    path = Path(identity_path)
    if path.suffix == ".sqlite":
        return path
    if path.name == "state.json":
        return path.with_name("conversation_state.sqlite")
    return path.with_suffix(".sqlite")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    return json.loads(raw)


class StateSqlite:
    """Own the L2 SQLite file for one ConversationStateStore identity."""

    def __init__(self, identity_path: Path) -> None:
        self.identity_path = Path(identity_path)
        self.identity_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = resolve_db_path(self.identity_path)
        self.json_path = (
            self.identity_path
            if self.identity_path.suffix == ".json"
            else self.identity_path.with_suffix(".json")
        )
        self._lock = threading.Lock()
        self._connect()
        self._migrate_json_if_needed()

    def _connect(self) -> None:
        self.conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA_SQL)
        try:
            self.conn.executescript(FTS_SQL)
        except sqlite3.OperationalError as exc:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_NOT_READY",
                    "SQLite FTS5 is required for conversation-state lookup",
                    error=str(exc),
                )
            ) from exc
        row = self.conn.execute(
            "SELECT value FROM state_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO state_meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
        elif str(row["value"]) != SCHEMA_VERSION:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_NOT_READY",
                    "unsupported conversation-state schema version",
                    schema_version=str(row["value"]),
                    expected=SCHEMA_VERSION,
                )
            )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _json_is_live(self) -> bool:
        return self.json_path.exists() and self.json_path.suffix == ".json"

    def _sqlite_has_documents(self) -> bool:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM state_documents").fetchone()
        return int(row["n"] if row else 0) > 0

    def _migrate_json_if_needed(self) -> None:
        if not self._json_is_live():
            return
        data = locked_read_json(self.json_path, default={"documents": {}})
        if not isinstance(data, dict):
            data = {"documents": {}}
        documents = data.get("documents") or {}
        versions = data.get("versions") or {}
        idempotency = data.get("idempotency_keys") or {}
        has_payload = bool(documents) or bool(versions) or bool(idempotency)
        if self._sqlite_has_documents():
            if has_payload:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_NOT_READY",
                        "conversation state has both a live JSON file and a SQLite store",
                        json_path=str(self.json_path),
                        sqlite_path=str(self.db_path),
                    )
                )
            return
        if not has_payload:
            archived = self.json_path.with_name(self.json_path.name + ".migrated")
            if not archived.exists():
                self.json_path.replace(archived)
            return
        with self.transaction() as conn:
            for session_id, doc in documents.items():
                state = dict(doc.get("state") or {"schema_version": 1})
                version = int(doc.get("version") or 0)
                watermark = doc.get("watermark_turn_id")
                session_versions = list(versions.get(session_id) or [])
                if session_versions:
                    seq = 0
                    for row in session_versions:
                        ops = list(row.get("operations") or [])
                        seq += 1
                        for index, op in enumerate(ops):
                            conn.execute(
                                """
                                INSERT INTO state_events(
                                    session_id, seq, source_turn_id, op_index,
                                    op_json, evidence_hash
                                ) VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    session_id,
                                    seq,
                                    str(row.get("source_turn_id") or row.get("watermark_turn_id") or ""),
                                    index,
                                    _dumps(op),
                                    evidence_hash(op if isinstance(op, dict) else {}),
                                ),
                            )
                        conn.execute(
                            """
                            INSERT INTO state_versions(
                                session_id, version, parent_version, watermark_turn_id,
                                source_turn_id, ops_json, operations_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                session_id,
                                int(row.get("version") or seq),
                                int(row.get("parent_version") or 0),
                                row.get("watermark_turn_id"),
                                row.get("source_turn_id"),
                                _dumps(row.get("ops") or []),
                                _dumps(ops),
                            ),
                        )
                    event_seq = seq
                else:
                    conn.execute(
                        """
                        INSERT INTO state_events(
                            session_id, seq, source_turn_id, op_index,
                            op_json, evidence_hash
                        ) VALUES (?, 1, ?, 0, ?, ?)
                        """,
                        (
                            session_id,
                            str(watermark or ""),
                            _dumps({"op": "import_snapshot", "state": state}),
                            evidence_hash({}),
                        ),
                    )
                    event_seq = 1
                conn.execute(
                    """
                    INSERT INTO state_documents(
                        session_id, version, watermark_turn_id, event_seq,
                        projection_hash, state_json, migrated_from
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        version,
                        str(watermark) if watermark else None,
                        event_seq,
                        canonical_state_hash(state),
                        _dumps(state),
                        str(self.json_path.name),
                    ),
                )
                replace_projection(conn, session_id, state)
            for scoped_key, result in idempotency.items():
                if ":" not in str(scoped_key):
                    continue
                session_id, key = str(scoped_key).split(":", 1)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO state_idempotency(session_id, key, result_json)
                    VALUES (?, ?, ?)
                    """,
                    (session_id, key, _dumps(result)),
                )
            conn.execute(
                "INSERT OR REPLACE INTO state_meta(key, value) VALUES ('migrated_from', ?)",
                (str(self.json_path.name),),
            )
        archived = self.json_path.with_name(self.json_path.name + ".migrated")
        self.json_path.replace(archived)

    def list_session_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT session_id FROM state_documents").fetchall()
        return [str(row["session_id"]) for row in rows]

    def get_document(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM state_documents WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "version": int(row["version"] or 0),
            "watermark_turn_id": row["watermark_turn_id"],
            "event_seq": int(row["event_seq"] or 0),
            "projection_hash": row["projection_hash"],
            "state": _loads(row["state_json"], {}),
        }

    def list_versions(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT version, parent_version, watermark_turn_id, source_turn_id,
                   ops_json, operations_json
            FROM state_versions
            WHERE session_id = ?
            ORDER BY version ASC
            """,
            (session_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "version": int(row["version"]),
                    "parent_version": int(row["parent_version"] or 0),
                    "watermark_turn_id": row["watermark_turn_id"],
                    "source_turn_id": row["source_turn_id"],
                    "ops": _loads(row["ops_json"], []),
                    "operations": _loads(row["operations_json"], []),
                }
            )
        return out

    def get_idempotent(self, session_id: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT result_json FROM state_idempotency WHERE session_id = ? AND key = ?",
            (session_id, key),
        ).fetchone()
        if row is None:
            return None
        payload = _loads(row["result_json"], {})
        return payload if isinstance(payload, dict) else None

    def persist_apply(
        self,
        *,
        session_id: str,
        state: dict[str, Any],
        operations: list[dict[str, Any]],
        source_turn_id: str,
        current_version: int,
        new_version: int,
        current_event_seq: int,
        idempotency_key: str,
        idempotency_result: dict[str, Any],
    ) -> int:
        result = self.apply_in_transaction(
            session_id=session_id,
            expected_parent_version=current_version,
            idempotency_key=idempotency_key,
            source_turn_id=source_turn_id,
            operations=operations,
            mutate=lambda _current: state,
        )
        return int(result["event_seq"])

    def apply_in_transaction(
        self,
        *,
        session_id: str,
        expected_parent_version: int | None,
        idempotency_key: str,
        source_turn_id: str,
        operations: list[dict[str, Any]],
        mutate: Any,
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT result_json FROM state_idempotency WHERE session_id = ? AND key = ?",
                    (session_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    payload = _loads(existing["result_json"], {})
                    if isinstance(payload, dict):
                        payload["idempotent_replay"] = True
                        return payload
            row = conn.execute(
                "SELECT * FROM state_documents WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            current_version = int(row["version"] or 0) if row is not None else 0
            current_event_seq = int(row["event_seq"] or 0) if row is not None else 0
            current_state = _loads(row["state_json"], {}) if row is not None else {}
            if expected_parent_version is not None and expected_parent_version != current_version:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_NOT_READY",
                        "state version conflict (CAS parent mismatch)",
                        expected_parent_version=expected_parent_version,
                        current_version=current_version,
                    )
                )
            state = mutate(copy.deepcopy(current_state) if current_state else {})
            new_version = current_version + 1
            event_seq = current_event_seq + 1
            for index, op in enumerate(operations):
                conn.execute(
                    """
                    INSERT INTO state_events(
                        session_id, seq, source_turn_id, op_index,
                        op_json, evidence_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        event_seq,
                        source_turn_id,
                        index,
                        _dumps(op),
                        evidence_hash(op),
                    ),
                )
            conn.execute(
                """
                INSERT INTO state_versions(
                    session_id, version, parent_version, watermark_turn_id,
                    source_turn_id, ops_json, operations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    new_version,
                    current_version,
                    source_turn_id,
                    source_turn_id,
                    _dumps([str(op.get("op")) for op in operations]),
                    _dumps(operations),
                ),
            )
            conn.execute(
                """
                INSERT INTO state_documents(
                    session_id, version, watermark_turn_id, event_seq,
                    projection_hash, state_json, migrated_from
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    version = excluded.version,
                    watermark_turn_id = excluded.watermark_turn_id,
                    event_seq = excluded.event_seq,
                    projection_hash = excluded.projection_hash,
                    state_json = excluded.state_json
                """,
                (
                    session_id,
                    new_version,
                    source_turn_id,
                    event_seq,
                    canonical_state_hash(state),
                    _dumps(state),
                ),
            )
            replace_projection(conn, session_id, state)
            result = {
                "decision": "apply",
                "state": state,
                "ops": len(operations),
                "version": new_version,
                "parent_version": current_version,
                "event_seq": event_seq,
            }
            if idempotency_key:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO state_idempotency(session_id, key, result_json)
                    VALUES (?, ?, ?)
                    """,
                    (
                        session_id,
                        idempotency_key,
                        _dumps(
                            {
                                "decision": "apply",
                                "ops": len(operations),
                                "version": new_version,
                                "parent_version": current_version,
                            }
                        ),
                    ),
                )
            return result

    def list_projection_items(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT kind, ref, payload_json, source_turn_id, status
            FROM state_projection_items
            WHERE session_id = ?
            ORDER BY kind, ref
            """,
            (session_id,),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            items.append(
                {
                    "kind": row["kind"],
                    "ref": row["ref"],
                    "payload": payload if isinstance(payload, dict) else {},
                    "source_turn_id": row["source_turn_id"] or "",
                    "status": row["status"] or "active",
                }
            )
        return items

    def list_collection_members(
        self, session_id: str, collection_ref: str
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT position, member_key
            FROM state_collection_members
            WHERE session_id = ? AND collection_ref = ?
            ORDER BY position ASC, member_key ASC
            """,
            (session_id, collection_ref),
        ).fetchall()
        return [
            {"position": int(row["position"]), "member_key": row["member_key"]}
            for row in rows
        ]

    def member_count(self, session_id: str, collection_ref: str | None = None) -> int:
        if collection_ref is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM state_collection_members WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS n FROM state_collection_members
                WHERE session_id = ? AND collection_ref = ?
                """,
                (session_id, collection_ref),
            ).fetchone()
        return int(row["n"] if row else 0)

    def get_item(
        self, session_id: str, ref: str, *, kind: str | None = None
    ) -> dict[str, Any] | None:
        if kind:
            row = self.conn.execute(
                """
                SELECT kind, ref, payload_json, source_turn_id, status
                FROM state_projection_items
                WHERE session_id = ? AND ref = ? AND kind = ?
                """,
                (session_id, ref, kind),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT kind, ref, payload_json, source_turn_id, status
                FROM state_projection_items
                WHERE session_id = ? AND ref = ?
                ORDER BY CASE kind
                    WHEN 'collection' THEN 0
                    WHEN 'entity' THEN 1
                    ELSE 2
                END
                """,
                (session_id, ref),
            ).fetchone()
        if row is None:
            return None
        payload = _loads(row["payload_json"], {})
        return {
            "kind": row["kind"],
            "ref": row["ref"],
            "payload": payload if isinstance(payload, dict) else {},
            "source_turn_id": row["source_turn_id"] or "",
            "status": row["status"] or "active",
        }

    def search_items(
        self, session_id: str, query: str, *, limit: int
    ) -> list[dict[str, Any]]:
        terms = lexical_terms(query)
        if not terms:
            return []
        match = " AND ".join(_fts_quote(term) for term in terms[:8])
        rows = []
        if match:
            try:
                rows = self.conn.execute(
                    """
                    SELECT i.kind, i.ref, i.payload_json, i.source_turn_id, i.status
                    FROM state_fts AS f
                    JOIN state_projection_items AS i
                      ON i.session_id = f.session_id AND i.ref = f.ref
                    WHERE f.session_id = ? AND state_fts MATCH ?
                    LIMIT ?
                    """,
                    (session_id, match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like_clauses = []
            params: list[Any] = [session_id]
            for term in terms[:4]:
                like_clauses.append("(ref LIKE ? OR payload_json LIKE ?)")
                pattern = f"%{term}%"
                params.extend([pattern, pattern])
            sql = f"""
                SELECT kind, ref, payload_json, source_turn_id, status
                FROM state_projection_items
                WHERE session_id = ? AND ({' OR '.join(like_clauses)})
                LIMIT ?
            """
            params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row["payload_json"], {})
            if str(row["status"] or "active") in {"superseded", "expired"}:
                continue
            items.append(
                {
                    "kind": row["kind"],
                    "ref": row["ref"],
                    "payload": payload if isinstance(payload, dict) else {},
                    "source_turn_id": row["source_turn_id"] or "",
                    "status": row["status"] or "active",
                }
            )
        return items


def replace_projection(conn: sqlite3.Connection, session_id: str, state: dict[str, Any]) -> None:
    conn.execute("DELETE FROM state_projection_items WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM state_collection_members WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM state_fts WHERE session_id = ?", (session_id,))
    entities = state.get("entities") or {}
    for eid, ent in entities.items():
        if not isinstance(ent, dict):
            continue
        entity_ref = str(eid)
        aliases = [str(a) for a in (ent.get("aliases") or [])]
        payload = {
            "type": ent.get("type") or "generic",
            "status": ent.get("status") or "active",
            "status_authority": ent.get("status_authority") or "model_inferred",
            "aliases": aliases,
        }
        _insert_item(
            conn,
            session_id=session_id,
            kind="entity",
            ref=entity_ref,
            payload=payload,
            source_turn_id=str(ent.get("status_source_turn_id") or ""),
            status=str(ent.get("status") or "active"),
            body=" ".join([entity_ref, *aliases, str(payload["type"])]),
        )
        for key, attr in (ent.get("attributes") or {}).items():
            if not isinstance(attr, dict):
                continue
            fact_ref = f"{entity_ref}.{key}"
            fact_payload = {
                "entity_id": entity_ref,
                "key": str(key),
                "value": attr.get("value"),
                "authority": attr.get("authority") or "model_inferred",
                "memory_type": attr.get("memory_type") or "fact",
            }
            _insert_item(
                conn,
                session_id=session_id,
                kind="fact",
                ref=fact_ref,
                payload=fact_payload,
                source_turn_id=str(attr.get("source_turn_id") or ""),
                status=str(attr.get("status") or "active"),
                body=" ".join(
                    [
                        fact_ref,
                        entity_ref,
                        str(key),
                        str(attr.get("value") or ""),
                        *aliases,
                    ]
                ),
            )
    for rel_name, edges in (state.get("relations") or {}).items():
        for edge in edges or []:
            if not isinstance(edge, dict):
                continue
            left = str(edge.get("from") or "")
            right = str(edge.get("to") or "")
            rel_ref = f"{rel_name}:{left}->{right}"
            _insert_item(
                conn,
                session_id=session_id,
                kind="relation",
                ref=rel_ref,
                payload={"relation": str(rel_name), "from": left, "to": right},
                source_turn_id="",
                status="active",
                body=f"{rel_name} {left} {right}",
            )
    for cname, coll in (state.get("collections") or {}).items():
        members = [str(m) for m in ((coll or {}).get("members") or [])]
        _insert_item(
            conn,
            session_id=session_id,
            kind="collection",
            ref=str(cname),
            payload={"member_count": len(members)},
            source_turn_id="",
            status="active",
            body=f"{cname} " + " ".join(members),
        )
        for position, member in enumerate(members):
            conn.execute(
                """
                INSERT INTO state_collection_members(
                    session_id, collection_ref, position, member_key
                ) VALUES (?, ?, ?, ?)
                """,
                (session_id, str(cname), position, member),
            )


def _insert_item(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    kind: str,
    ref: str,
    payload: dict[str, Any],
    source_turn_id: str,
    status: str,
    body: str,
) -> None:
    conn.execute(
        """
        INSERT INTO state_projection_items(
            session_id, kind, ref, payload_json, source_turn_id, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (session_id, kind, ref, _dumps(payload), source_turn_id, status),
    )
    conn.execute(
        "INSERT INTO state_fts(session_id, kind, ref, body) VALUES (?, ?, ?, ?)",
        (session_id, kind, ref, body),
    )


def _fts_quote(term: str) -> str:
    escaped = term.replace('"', '""')
    return f'"{escaped}"'
