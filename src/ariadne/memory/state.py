from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .state_sqlite import StateSqlite
from .working_set import (
    WorkingSetResult,
    assemble_working_set,
    decode_cursor,
    encode_cursor,
    query_hash,
    render_item,
)

ALLOWED_OPS = {
    "ensure_entity",
    "set_alias",
    "set_attribute",
    "expire_attribute",
    "set_status",
    "set_relation",
    "remove_relation",
    "ensure_collection",
    "collection_append",
    "collection_remove",
    "collection_move",
    "set_current_goal",
    "bind_task_goal",
}

ATTRIBUTE_AUTHORITIES = {
    "model_inferred": 0,
    "tool_observed": 1,
    "user_explicit": 2,
}
STATUS_AUTHORITIES = {
    **ATTRIBUTE_AUTHORITIES,
    "verified_check": 3,
}
ATTRIBUTE_MEMORY_TYPES = {"fact", "preference", "goal", "hypothesis"}
ATTRIBUTE_STATUSES = {"active", "superseded", "expired"}
ENTITY_STATUSES = {"active", "done", "cancelled", "archived"}
TERMINAL_ENTITY_STATUSES = {"done", "cancelled", "archived"}
CURRENT_GOAL_POINTER_ID = "session:current_goal"
CURRENT_GOAL_ATTRIBUTE = "goal_id"

MAX_ENTITIES = 4_096
MAX_RELATIONS_PER_TYPE = 256
MAX_RELATION_TYPES = 64
MAX_COLLECTION_MEMBERS = 4_096
MAX_COLLECTIONS = 128
LOOKUP_RESPONSE_BYTES_MAX = 16_000

# Goal identities are a persistence protocol.  Keep construction and
# validation in one place so lifecycle-bearing goals cannot drift between
# TurnApplication, automatic capture, and StateStore.
GOAL_ID_PREFIX = "goal:"
LEGACY_GOAL_ID_PREFIX = "goal:legacy:"


def is_goal_id(value: Any) -> bool:
    """Return whether *value* is a non-empty lifecycle Goal identity."""

    return isinstance(value, str) and value.startswith(GOAL_ID_PREFIX) and len(value) > len(
        GOAL_ID_PREFIX
    )


def make_goal_id(seed: str) -> str:
    """Construct a canonical immutable Goal id from a host-provided seed."""

    normalized = str(seed or "").strip()
    if normalized.startswith(GOAL_ID_PREFIX):
        normalized = normalized[len(GOAL_ID_PREFIX) :]
    if not normalized:
        raise AriadneError(
            app_error(
                "ARIADNE_MEMORY_GOAL_BINDING",
                "goal id seed must be non-empty",
                seed=normalized,
            )
        )
    return f"{GOAL_ID_PREFIX}{normalized}"


def make_legacy_goal_id(encoded: bytes) -> str:
    """Derive the stable id used when migrating the pre-pointer Goal."""

    return f"{LEGACY_GOAL_ID_PREFIX}{hashlib.sha256(encoded).hexdigest()[:20]}"


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "entities": {},
        "relations": {},
        "collections": {},
        # Host-owned and intentionally omitted from model-facing rendering.
        "task_goal_bindings": {},
    }


@dataclass
class ConversationStateStore:
    """Authoritative L2 state. Events and projection live in SQLite."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = StateSqlite(self.path)

    @property
    def store_identity(self) -> str:
        """Stable opaque identity used to fence cross-store journal replay."""

        digest = hashlib.sha256(
            str(self.path.resolve()).encode("utf-8")
        ).hexdigest()
        return f"conversation-state-v1:{digest}"

    def get(self, session_id: str) -> dict[str, Any]:
        doc = self._db.get_document(session_id)
        if not doc:
            return empty_state()
        state = doc.get("state") or empty_state()
        return dict(state) if isinstance(state, dict) else empty_state()

    def _read(self) -> dict[str, Any]:
        """Reconstruct the legacy document map for tests and debug inspection."""

        documents: dict[str, Any] = {}
        versions: dict[str, Any] = {}
        for session_id in self._db.list_session_ids():
            doc = self._db.get_document(session_id)
            if doc is None:
                continue
            documents[session_id] = {
                "state": doc.get("state") or empty_state(),
                "version": int(doc.get("version") or 0),
                "watermark_turn_id": doc.get("watermark_turn_id"),
            }
            versions[session_id] = self._db.list_versions(session_id)
        return {"documents": documents, "versions": versions}

    @staticmethod
    def model_safe_state(state: dict[str, Any]) -> dict[str, Any]:
        """Return the state view that may cross the model-facing boundary.

        Task→goal bindings are Host-owned routing metadata.  They are needed
        by task completion and journal recovery, but exposing them to the model
        would disclose internal identities and invite forged binding attempts.
        Keep this projection explicit so new Host-only fields cannot leak just
        because a caller serialized ``get()`` directly.
        """

        safe = copy.deepcopy(state)
        safe.pop("task_goal_bindings", None)
        return safe

    def get_model_safe(self, session_id: str) -> dict[str, Any]:
        return self.model_safe_state(self.get(session_id))

    @staticmethod
    def current_goal_id_from_state(state: dict[str, Any]) -> str | None:
        entities = state.get("entities") or {}
        pointer = entities.get(CURRENT_GOAL_POINTER_ID)
        if not isinstance(pointer, dict):
            return None
        if pointer.get("type") == "goal_pointer":
            payload = (pointer.get("attributes") or {}).get(CURRENT_GOAL_ATTRIBUTE)
            if not isinstance(payload, dict) or payload.get("status") != "active":
                return None
            goal_id = str(payload.get("value") or "")
            goal = entities.get(goal_id)
            if goal_id and isinstance(goal, dict) and goal.get("type") == "goal":
                return goal_id
            return None
        # Pre-pointer schema used the fixed id as the lifecycle-bearing Goal.
        if pointer.get("type") == "goal":
            return CURRENT_GOAL_POINTER_ID
        return None

    def current_goal_id(self, session_id: str) -> str | None:
        return self.current_goal_id_from_state(self.get(session_id))

    def goal_id_for_task(self, session_id: str, task_id: str) -> str | None:
        """Resolve only the immutable Host-owned task→goal binding."""

        tid = (task_id or "").strip()
        if not tid:
            return None
        bindings = self.get(session_id).get("task_goal_bindings") or {}
        value = bindings.get(tid)
        if value is None:
            return None
        if isinstance(value, list) or isinstance(value, dict):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_GOAL_BINDING",
                    "task has multiple or malformed Host-owned goal bindings",
                    session_id=session_id,
                    task_id=tid,
                )
            )
        goal_id = str(value).strip()
        if not goal_id:
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_GOAL_BINDING",
                    "task has an empty Host-owned goal binding",
                    session_id=session_id,
                    task_id=tid,
                )
            )
        if not is_goal_id(goal_id):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_GOAL_BINDING",
                    "Host task-goal binding contains a non-canonical goal id",
                    session_id=session_id,
                    task_id=tid,
                    goal_id=goal_id,
                )
            )
        return goal_id

    def bind_task_goal(
        self,
        *,
        session_id: str,
        task_id: str,
        goal_id: str,
        source_turn_id: str,
        evidence_text: str,
        idempotency_key: str | None = None,
        goal_description: str = "",
    ) -> dict[str, Any]:
        """Persist an immutable task→goal binding through a Host-only path."""

        tid = str(task_id or "").strip()
        gid = str(goal_id or "").strip()
        if not tid or not is_goal_id(gid):
            raise AriadneError(
                app_error(
                    "ARIADNE_MEMORY_GOAL_BINDING",
                    "Host task-goal binding requires non-empty task and goal ids",
                    task_id=tid,
                    goal_id=gid,
                )
            )
        quote = str(evidence_text or "")[:2000]
        operations: list[dict[str, Any]] = [
            {
                "op": "bind_task_goal",
                "task_id": tid,
                "goal_id": gid,
                "evidence_quote": quote,
            },
            {
                "op": "ensure_entity",
                "entity_id": gid,
                "type": "goal",
                "evidence_quote": quote,
            },
            {
                "op": "set_status",
                "entity_id": gid,
                "status": "active",
                "authority": "user_explicit",
                "evidence_quote": quote,
            },
            {
                "op": "set_current_goal",
                "goal_id": gid,
                "authority": "user_explicit",
                "evidence_quote": quote,
            },
        ]
        if str(goal_description or "").strip():
            operations.insert(
                2,
                {
                    "op": "set_attribute",
                    "entity_id": gid,
                    "key": "description",
                    "value": str(goal_description).strip()[:2000],
                    "memory_type": "goal",
                    "authority": "user_explicit",
                    "evidence_quote": quote,
                },
            )
        return self.apply_ops(
            session_id=session_id,
            operations=operations,
            source_turn_id=source_turn_id,
            evidence_text=evidence_text,
            idempotency_key=idempotency_key,
            host_owned=True,
        )

    def get_as_of(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> dict[str, Any]:
        """Point-in-time state, restoring superseded values valid at the cutoff.

        When ``allowed_turn_ids`` is None, returns current state. Attributes
        without ``source_turn_id`` are kept (conservative). Entities that end
        up with no attributes remain if they still have aliases or type.
        """
        state = self.get(session_id)
        if allowed_turn_ids is None:
            return state
        versions = self.list_versions(session_id)
        # New-format versions retain the complete closed operations. Replaying
        # them reconstructs relations, aliases, entity status and collections,
        # not only attributes. Mixed/legacy histories keep the conservative
        # attribute fallback below because their lost operation arguments cannot
        # be recovered honestly.
        if versions and all(
            isinstance(row.get("operations"), list)
            and all(isinstance(op, dict) for op in row.get("operations") or [])
            for row in versions
        ):
            replayed = empty_state()
            for row in versions:
                source_turn_id = str(row.get("source_turn_id") or row.get("watermark_turn_id") or "")
                if source_turn_id and source_turn_id not in allowed_turn_ids:
                    continue
                for op in row.get("operations") or []:
                    self._apply_one(
                        replayed,
                        copy.deepcopy(op),
                        source_turn_id=source_turn_id,
                    )
            return replayed
        filtered = copy.deepcopy(state)
        entities = filtered.get("entities") or {}
        for _eid, ent in list(entities.items()):
            attrs = ent.get("attributes") or {}
            kept: dict[str, Any] = {}
            for key, payload in attrs.items():
                if not isinstance(payload, dict):
                    kept[key] = payload
                    continue
                candidates = [
                    *(payload.get("history") or []),
                    {k: v for k, v in payload.items() if k != "history"},
                ]
                valid = [
                    candidate
                    for candidate in candidates
                    if not str(candidate.get("source_turn_id") or "").strip()
                    or str(candidate.get("source_turn_id")) in allowed_turn_ids
                ]
                if not valid:
                    continue
                restored = copy.deepcopy(valid[-1])
                older = [copy.deepcopy(candidate) for candidate in valid[:-1]]
                if restored.get("status") == "superseded":
                    restored["status"] = "active"
                    restored.pop("superseded_by", None)
                restored["history"] = older
                kept[key] = restored
            ent["attributes"] = kept
        return filtered

    def render(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> tuple[str, int]:
        state = (
            self.get(session_id)
            if allowed_turn_ids is None
            else self.get_as_of(session_id, allowed_turn_ids=allowed_turn_ids)
        )
        return self.render_state(state)

    def render_model_safe(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> tuple[str, int]:
        """Render one Host-safe snapshot for model prompt/context use."""

        _state, text, count = self.render_model_safe_snapshot(
            session_id, allowed_turn_ids=allowed_turn_ids
        )
        return text, count

    def render_model_safe_snapshot(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> tuple[dict[str, Any], str, int]:
        """Return the safe JSON view and its rendering from one read.

        Model-facing tools need both structured state and text. Keeping both
        values tied to the same snapshot prevents a concurrent Host update
        from making the returned JSON and rendered text disagree.
        """

        state = (
            self.get_model_safe(session_id)
            if allowed_turn_ids is None
            else self.model_safe_state(
                self.get_as_of(session_id, allowed_turn_ids=allowed_turn_ids)
            )
        )
        text, count = self.render_state(state)
        return state, text, count

    def version(self, session_id: str) -> int:
        """Current document version (0 when never projected)."""
        doc = self._db.get_document(session_id)
        if not doc:
            return 0
        return int(doc.get("version") or 0)

    def event_seq(self, session_id: str) -> int:
        doc = self._db.get_document(session_id)
        if not doc:
            return 0
        return int(doc.get("event_seq") or 0)

    def list_versions(self, session_id: str) -> list[dict[str, Any]]:
        """Append-only projection history for the session."""
        return self._db.list_versions(session_id)

    def watermark(self, session_id: str) -> str | None:
        """Turn id of the last succeeded projection, or None if never projected."""
        doc = self._db.get_document(session_id)
        if not doc:
            return None
        wm = doc.get("watermark_turn_id")
        return str(wm) if wm else None

    def render_state(self, state: dict[str, Any]) -> tuple[str, int]:
        entities = state.get("entities") or {}
        if not entities and not (state.get("collections") or {}) and not (state.get("relations") or {}):
            return "", 0
        lines = ["[CONVERSATION_STATE: AUTHORITATIVE]"]
        for eid, ent in entities.items():
            status = ent.get("status") or "active"
            aliases = ", ".join(ent.get("aliases") or []) or "-"
            status_authority = ent.get("status_authority") or "model_inferred"
            lines.append(
                f"- {eid} ({ent.get('type') or 'generic'}) status={status} "
                f"status_authority={status_authority} aliases=[{aliases}]"
            )
            attrs = ent.get("attributes") or {}
            for key, payload in attrs.items():
                if isinstance(payload, dict):
                    lines.append(
                        f"    {key}={payload.get('value')!r} "
                        f"status={payload.get('status') or 'active'} "
                        f"type={payload.get('memory_type') or 'fact'} "
                        f"authority={payload.get('authority') or 'model_inferred'} "
                        f"source_turn={payload.get('source_turn_id')}"
                    )
                else:
                    lines.append(f"    {key}={payload!r}")
        for cname, coll in (state.get("collections") or {}).items():
            members = coll.get("members") or []
            lines.append(f"- collection {cname}: {members}")
        for rel_name, rels in (state.get("relations") or {}).items():
            for rel in rels:
                lines.append(f"- relation {rel_name}: {rel.get('from')} -> {rel.get('to')}")
        text = "\n".join(lines)
        return text, len(entities)

    def assemble_working_set(
        self,
        session_id: str,
        query: str,
        *,
        soft_chars: int,
        hard_chars: int,
        allowed_turn_ids: set[str] | None = None,
    ) -> WorkingSetResult:
        if allowed_turn_ids is not None:
            state = self.model_safe_state(
                self.get_as_of(session_id, allowed_turn_ids=allowed_turn_ids)
            )
            items = _projection_items_from_state(state)
            member_count = sum(
                len((coll or {}).get("members") or [])
                for coll in (state.get("collections") or {}).values()
            )
        else:
            items = self._db.list_projection_items(session_id)
            items = [item for item in items if str(item.get("ref") or "") != "task_goal_bindings"]
            member_count = self._db.member_count(session_id)
        return assemble_working_set(
            items,
            query=query,
            projection_seq=self.event_seq(session_id),
            soft_chars=soft_chars,
            hard_chars=hard_chars,
            member_count=member_count,
        )

    def lookup(
        self,
        *,
        session_id: str,
        query: str,
        limit: int = 10,
        cursor: str = "",
    ) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "query is required"))
        if limit < 1:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "limit must be >= 1"))
        if limit > 32:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    f"limit {limit} exceeds hard max 32",
                    limit=limit,
                    limit_max=32,
                )
            )
        seq = self.event_seq(session_id)
        offset = 0
        if cursor:
            payload = decode_cursor(cursor)
            if str(payload.get("session_id") or "") != session_id:
                raise AriadneError(
                    app_error("ARIADNE_MEMORY_STATE_CURSOR_STALE", "lookup cursor session mismatch")
                )
            if int(payload.get("projection_seq") or 0) != seq:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_STATE_CURSOR_STALE",
                        "conversation state changed since this lookup cursor was issued",
                    )
                )
            if str(payload.get("query_hash") or "") != query_hash(q):
                raise AriadneError(
                    app_error("ARIADNE_MEMORY_STATE_CURSOR_STALE", "lookup cursor query mismatch")
                )
            offset = int(payload.get("offset") or 0)
        collection = self._db.get_item(session_id, q, kind="collection")
        exact = collection or self._db.get_item(session_id, q)
        collection_members = self._db.list_collection_members(session_id, q)
        if collection is not None or collection_members:
            page = collection_members[offset : offset + limit]
            rendered: list[dict[str, Any]] = []
            for row in page:
                rendered.append(
                    {
                        "kind": "collection_member",
                        "ref": f"{q}#{row['member_key']}",
                        "collection": q,
                        "member": row["member_key"],
                        "position": row["position"],
                    }
                )
            return self._finish_lookup(
                session_id=session_id,
                query=q,
                seq=seq,
                offset=offset,
                limit=limit,
                items=rendered,
                total=len(collection_members),
            )
        items: list[dict[str, Any]] = []
        if exact is not None and str(exact.get("status") or "active") not in {
            "superseded",
            "expired",
        }:
            items.append(exact)
            if exact.get("kind") == "entity":
                for fact in self._db.list_projection_items(session_id):
                    if (
                        fact.get("kind") == "fact"
                        and (fact.get("payload") or {}).get("entity_id") == exact.get("ref")
                        and str(fact.get("status") or "active") not in {"superseded", "expired"}
                    ):
                        items.append(fact)
        else:
            items = self._db.search_items(session_id, q, limit=max(limit * 3, limit))
        page_items = items[offset : offset + limit]
        serialized = []
        for item in page_items:
            serialized.append(
                {
                    "kind": item.get("kind"),
                    "ref": item.get("ref"),
                    "status": item.get("status") or "active",
                    "payload": item.get("payload") or {},
                    "text": render_item(item),
                }
            )
        return self._finish_lookup(
            session_id=session_id,
            query=q,
            seq=seq,
            offset=offset,
            limit=limit,
            items=serialized,
            total=len(items),
        )

    def _finish_lookup(
        self,
        *,
        session_id: str,
        query: str,
        seq: int,
        offset: int,
        limit: int,
        items: list[dict[str, Any]],
        total: int,
    ) -> dict[str, Any]:
        kept = list(items)
        while kept:
            body = json.dumps(kept, ensure_ascii=False)
            if len(body.encode("utf-8")) <= LOOKUP_RESPONSE_BYTES_MAX:
                break
            if len(kept) == 1:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_LOOKUP_ITEM_TOO_LARGE",
                        "a single lookup item exceeds the page byte cap",
                    )
                )
            kept.pop()
        items = kept
        next_offset = offset + len(items)
        has_more = next_offset < total
        next_cursor = ""
        if has_more:
            next_cursor = encode_cursor(
                {
                    "session_id": session_id,
                    "projection_seq": seq,
                    "query_hash": query_hash(query),
                    "offset": next_offset,
                }
            )
        return {
            "query": query,
            "items": items,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "projection_seq": seq,
            "semantic_status": "disabled",
        }

    def apply_ops(
        self,
        *,
        session_id: str,
        operations: list[dict[str, Any]],
        source_turn_id: str,
        evidence_text: str,
        expected_parent_version: int | None = None,
        idempotency_key: str | None = None,
        host_owned: bool = False,
    ) -> dict[str, Any]:
        if not operations:
            raise AriadneError(
                app_error(
                    "ARIADNE_INVALID_TOOL_ARGS",
                    "state apply requires at least one operation; use an explicit projector decision for no change",
                )
            )
        # Validate ops against evidence before locking (no invent / no bad quotes).
        evidence = evidence_text or ""
        for op in operations:
            name = str(op.get("op") or "").strip()
            if name not in ALLOWED_OPS:
                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", f"unknown state op: {name}"))
            required_fields = {
                "ensure_entity": ("entity_id",),
                "set_alias": ("entity_id", "alias"),
                "set_attribute": ("entity_id", "key"),
                "expire_attribute": ("entity_id", "key"),
                "set_status": ("entity_id", "status"),
                "set_relation": ("relation", "from", "to"),
                "remove_relation": ("relation", "from", "to"),
                "ensure_collection": ("name",),
                "collection_append": ("name", "member"),
                "collection_remove": ("name", "member"),
                "collection_move": ("name", "member", "to_index"),
                "set_current_goal": ("goal_id",),
                "bind_task_goal": ("task_id", "goal_id"),
            }[name]
            if name in {"set_current_goal", "bind_task_goal"} and not host_owned:
                raise AriadneError(
                    app_error(
                        "ARIADNE_TOOL_DENIED",
                        f"state op {name} is Host-owned",
                        op=name,
                    )
                )
            if (
                name == "set_attribute"
                and str(op.get("key") or "").strip() == "task_id"
                and not host_owned
            ):
                raise AriadneError(
                    app_error(
                        "ARIADNE_TOOL_DENIED",
                        "task_id is Host-owned and cannot be written by model-facing state",
                    )
                )
            missing = [field for field in required_fields if field not in op]
            if missing:
                raise AriadneError(
                    app_error(
                        "ARIADNE_INVALID_TOOL_ARGS",
                        f"state op {name} is missing required fields",
                        op=name,
                        missing=missing,
                    )
                )
            if name in {
                "set_attribute",
                "expire_attribute",
                "set_current_goal",
            }:
                authority = str(op.get("authority") or "model_inferred")
                if authority not in ATTRIBUTE_AUTHORITIES:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"unknown attribute authority: {authority}",
                            op=name,
                        )
                    )
            if name == "set_status":
                authority = str(op.get("authority") or "model_inferred")
                if authority not in STATUS_AUTHORITIES:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"unknown status authority: {authority}",
                            op=name,
                        )
                    )
                status = str(op.get("status") or "")
                if status not in ENTITY_STATUSES:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"unknown entity status: {status}",
                            op=name,
                        )
                    )
            if name == "set_attribute":
                memory_type = str(op.get("memory_type") or "fact")
                if memory_type not in ATTRIBUTE_MEMORY_TYPES:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"unknown memory_type: {memory_type}",
                            op=name,
                        )
                    )
            quote = str(op.get("evidence_quote") or "").strip()
            if not quote or quote not in evidence:
                raise AriadneError(
                    app_error(
                        "ARIADNE_INVALID_TOOL_ARGS",
                        "evidence_quote must occur in turn text",
                        op=name,
                    )
                )

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            state = current or empty_state()
            if "entities" not in state:
                state = empty_state() | state
            for op in operations:
                self._apply_one(state, op, source_turn_id=source_turn_id)
            if len(state.get("entities") or {}) > MAX_ENTITIES:
                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "entity capacity exceeded"))
            relations = state.get("relations") or {}
            if len(relations) > MAX_RELATION_TYPES:
                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "relation type capacity exceeded")
                )
            for rel_name, edges in relations.items():
                if len(edges or []) > MAX_RELATIONS_PER_TYPE:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"relation capacity exceeded for {rel_name!r}",
                            relation=rel_name,
                        )
                    )
            collections = state.get("collections") or {}
            if len(collections) > MAX_COLLECTIONS:
                raise AriadneError(
                    app_error("ARIADNE_INVALID_TOOL_ARGS", "collection capacity exceeded")
                )
            for cname, coll in collections.items():
                members = (coll or {}).get("members") or []
                if len(members) > MAX_COLLECTION_MEMBERS:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_INVALID_TOOL_ARGS",
                            f"collection member capacity exceeded for {cname!r}",
                            collection=cname,
                        )
                    )
            return state

        return self._db.apply_in_transaction(
            session_id=session_id,
            expected_parent_version=expected_parent_version,
            idempotency_key=str(idempotency_key or ""),
            source_turn_id=source_turn_id,
            operations=operations,
            mutate=mutate,
        )

    def _apply_one(self, state: dict[str, Any], op: dict[str, Any], *, source_turn_id: str) -> None:
        entities: dict[str, Any] = state.setdefault("entities", {})
        collections: dict[str, Any] = state.setdefault("collections", {})
        relations: dict[str, Any] = state.setdefault("relations", {})
        name = op["op"]
        if name == "bind_task_goal":
            task_id = str(op["task_id"]).strip()
            goal_id = str(op["goal_id"]).strip()
            bindings = state.setdefault("task_goal_bindings", {})
            existing = bindings.get(task_id)
            if existing is not None and str(existing) != goal_id:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_GOAL_BINDING",
                        "task already has a different immutable goal binding",
                        task_id=task_id,
                        existing_goal_id=str(existing),
                        proposed_goal_id=goal_id,
                    )
                )
            bindings[task_id] = goal_id
        elif name == "set_current_goal":
            goal_id = str(op["goal_id"])
            goal = entities.get(goal_id)
            if not isinstance(goal, dict) or goal.get("type") != "goal":
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "current-goal pointer target must be an existing goal",
                        goal_id=goal_id,
                    )
                )
            pointer = entities.get(CURRENT_GOAL_POINTER_ID)
            if isinstance(pointer, dict) and pointer.get("type") == "goal":
                encoded = json.dumps(
                    pointer,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
                legacy_goal_id = make_legacy_goal_id(encoded)
                if legacy_goal_id in entities:
                    raise AriadneError(
                        app_error(
                            "ARIADNE_MEMORY_CONFLICT",
                            "legacy goal migration target already exists",
                            goal_id=legacy_goal_id,
                        )
                    )
                entities[legacy_goal_id] = pointer
                del entities[CURRENT_GOAL_POINTER_ID]
                for collection in collections.values():
                    if not isinstance(collection, dict):
                        continue
                    collection["members"] = [
                        legacy_goal_id
                        if member == CURRENT_GOAL_POINTER_ID
                        else member
                        for member in collection.get("members") or []
                    ]
                for edges in relations.values():
                    for edge in edges or []:
                        if edge.get("from") == CURRENT_GOAL_POINTER_ID:
                            edge["from"] = legacy_goal_id
                        if edge.get("to") == CURRENT_GOAL_POINTER_ID:
                            edge["to"] = legacy_goal_id
            elif isinstance(pointer, dict) and pointer.get("type") != "goal_pointer":
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "current-goal pointer id is occupied by another entity type",
                        entity_type=str(pointer.get("type") or "generic"),
                    )
                )
            pointer = entities.setdefault(
                CURRENT_GOAL_POINTER_ID,
                {
                    "type": "goal_pointer",
                    "aliases": [],
                    "attributes": {},
                    "status": "active",
                },
            )
            pointer["type"] = "goal_pointer"
            self._apply_one(
                state,
                {
                    "op": "set_attribute",
                    "entity_id": CURRENT_GOAL_POINTER_ID,
                    "key": CURRENT_GOAL_ATTRIBUTE,
                    "value": goal_id,
                    "memory_type": "goal",
                    "authority": str(op.get("authority") or "model_inferred"),
                    "evidence_quote": str(op.get("evidence_quote") or ""),
                },
                source_turn_id=source_turn_id,
            )
        elif name == "set_relation":
            rel = str(op["relation"])
            edge = {"from": str(op["from"]), "to": str(op["to"])}
            edges = relations.setdefault(rel, [])
            # Dedupe identical edges (M08).
            if edge not in edges:
                edges.append(edge)
        elif name == "remove_relation":
            rel = str(op["relation"])
            edge = {"from": str(op["from"]), "to": str(op["to"])}
            edges = relations.setdefault(rel, [])
            relations[rel] = [e for e in edges if e != edge]
        elif name == "collection_move":
            cname = str(op["name"])
            coll = collections.setdefault(cname, {"members": []})
            member = str(op["member"])
            members = [m for m in (coll.get("members") or []) if m != member]
            to_index = max(0, min(int(op.get("to_index") or 0), len(members)))
            members.insert(to_index, member)
            coll["members"] = members
        elif name == "ensure_entity":
            eid = str(op["entity_id"])
            ent = entities.setdefault(
                eid,
                {"type": str(op.get("type") or "generic"), "aliases": [], "attributes": {}, "status": "active"},
            )
            if op.get("type"):
                ent["type"] = str(op["type"])
        elif name == "set_alias":
            eid = str(op["entity_id"])
            ent = entities.setdefault(
                eid, {"type": "generic", "aliases": [], "attributes": {}, "status": "active"}
            )
            alias = str(op["alias"])
            aliases = list(ent.get("aliases") or [])
            if alias not in aliases:
                aliases.append(alias)
            ent["aliases"] = aliases
        elif name == "set_attribute":
            eid = str(op["entity_id"])
            ent = entities.setdefault(
                eid, {"type": "generic", "aliases": [], "attributes": {}, "status": "active"}
            )
            key = str(op["key"])
            attrs = ent.setdefault("attributes", {})
            prior = attrs.get(key)
            authority = str(op.get("authority") or "model_inferred")
            memory_type = str(op.get("memory_type") or "fact")
            record_id = f"{source_turn_id}:{eid}:{key}"
            if isinstance(prior, dict):
                prior_authority = str(prior.get("authority") or "model_inferred")
                prior_status = str(prior.get("status") or "active")
                value_changed = prior.get("value") != op.get("value")
                if (
                    prior_status == "active"
                    and value_changed
                    and ATTRIBUTE_AUTHORITIES[authority]
                    < ATTRIBUTE_AUTHORITIES.get(prior_authority, 0)
                ):
                    raise AriadneError(
                        app_error(
                            "ARIADNE_MEMORY_CONFLICT",
                            "lower-authority evidence cannot overwrite an active attribute",
                            entity_id=eid,
                            key=key,
                            current_authority=prior_authority,
                            proposed_authority=authority,
                            current_source_turn_id=prior.get("source_turn_id"),
                        )
                    )
                if not value_changed and prior_status == "active":
                    return
                history = [copy.deepcopy(item) for item in (prior.get("history") or [])]
                snapshot = {
                    k: copy.deepcopy(v)
                    for k, v in prior.items()
                    if k != "history"
                }
                snapshot["status"] = "superseded"
                snapshot["superseded_by"] = record_id
                history.append(snapshot)
            else:
                history = []
            attrs[key] = {
                "record_id": record_id,
                "value": op.get("value"),
                "source_turn_id": source_turn_id,
                "evidence_quote": str(op.get("evidence_quote") or ""),
                "authority": authority,
                "memory_type": memory_type,
                "status": "active",
                "history": history,
            }
        elif name == "expire_attribute":
            eid = str(op["entity_id"])
            ent = entities.get(eid)
            key = str(op["key"])
            current = (ent.get("attributes") or {}).get(key) if ent else None
            if not isinstance(current, dict):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "cannot expire an attribute that does not exist",
                        entity_id=eid,
                        key=key,
                    )
                )
            authority = str(op.get("authority") or "model_inferred")
            current_authority = str(current.get("authority") or "model_inferred")
            if ATTRIBUTE_AUTHORITIES[authority] < ATTRIBUTE_AUTHORITIES.get(current_authority, 0):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "lower-authority evidence cannot expire an active attribute",
                        entity_id=eid,
                        key=key,
                        current_authority=current_authority,
                        proposed_authority=authority,
                    )
                )
            history = [copy.deepcopy(item) for item in (current.get("history") or [])]
            snapshot = {k: copy.deepcopy(v) for k, v in current.items() if k != "history"}
            snapshot["status"] = "superseded"
            expired_id = f"{source_turn_id}:{eid}:{key}:expired"
            snapshot["superseded_by"] = expired_id
            history.append(snapshot)
            current.update(
                {
                    "record_id": expired_id,
                    "status": "expired",
                    "source_turn_id": source_turn_id,
                    "evidence_quote": str(op.get("evidence_quote") or ""),
                    "authority": authority,
                    "history": history,
                }
            )
            current.pop("superseded_by", None)
        elif name == "set_status":
            eid = str(op["entity_id"])
            ent = entities.setdefault(
                eid, {"type": "generic", "aliases": [], "attributes": {}, "status": "active"}
            )
            proposed_status = str(op.get("status") or "active")
            proposed_authority = str(op.get("authority") or "model_inferred")
            current_status = str(ent.get("status") or "active")
            current_authority = str(
                ent.get("status_authority") or "model_inferred"
            )
            if proposed_status == current_status and STATUS_AUTHORITIES[
                proposed_authority
            ] <= STATUS_AUTHORITIES.get(current_authority, 0):
                return
            if (
                current_status in TERMINAL_ENTITY_STATUSES
                and proposed_status == "active"
            ):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "a terminal entity cannot be reactivated in place",
                        entity_id=eid,
                        current_status=current_status,
                        current_authority=current_authority,
                        proposed_authority=proposed_authority,
                    )
                )
            if STATUS_AUTHORITIES[proposed_authority] < STATUS_AUTHORITIES.get(
                current_authority, 0
            ):
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_CONFLICT",
                        "lower-authority evidence cannot overwrite entity status",
                        entity_id=eid,
                        current_status=current_status,
                        proposed_status=proposed_status,
                        current_authority=current_authority,
                        proposed_authority=proposed_authority,
                    )
                )
            ent["status"] = proposed_status
            ent["status_authority"] = proposed_authority
            ent["status_source_turn_id"] = source_turn_id
        elif name == "ensure_collection":
            cname = str(op["name"])
            collections.setdefault(cname, {"members": []})
        elif name == "collection_append":
            cname = str(op["name"])
            coll = collections.setdefault(cname, {"members": []})
            member = str(op["member"])
            members = list(coll.get("members") or [])
            if member not in members:
                members.append(member)
            coll["members"] = members
        elif name == "collection_remove":
            cname = str(op["name"])
            coll = collections.setdefault(cname, {"members": []})
            member = str(op["member"])
            coll["members"] = [m for m in (coll.get("members") or []) if m != member]


def _projection_items_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for eid, ent in (state.get("entities") or {}).items():
        if not isinstance(ent, dict):
            continue
        items.append(
            {
                "kind": "entity",
                "ref": str(eid),
                "payload": {
                    "type": ent.get("type") or "generic",
                    "status": ent.get("status") or "active",
                    "status_authority": ent.get("status_authority") or "model_inferred",
                    "aliases": list(ent.get("aliases") or []),
                },
                "source_turn_id": str(ent.get("status_source_turn_id") or ""),
                "status": str(ent.get("status") or "active"),
            }
        )
        for key, attr in (ent.get("attributes") or {}).items():
            if not isinstance(attr, dict):
                continue
            items.append(
                {
                    "kind": "fact",
                    "ref": f"{eid}.{key}",
                    "payload": {
                        "entity_id": str(eid),
                        "key": str(key),
                        "value": attr.get("value"),
                        "authority": attr.get("authority") or "model_inferred",
                        "memory_type": attr.get("memory_type") or "fact",
                    },
                    "source_turn_id": str(attr.get("source_turn_id") or ""),
                    "status": str(attr.get("status") or "active"),
                }
            )
    for rel_name, edges in (state.get("relations") or {}).items():
        for edge in edges or []:
            if not isinstance(edge, dict):
                continue
            left = str(edge.get("from") or "")
            right = str(edge.get("to") or "")
            items.append(
                {
                    "kind": "relation",
                    "ref": f"{rel_name}:{left}->{right}",
                    "payload": {"relation": str(rel_name), "from": left, "to": right},
                    "source_turn_id": "",
                    "status": "active",
                }
            )
    for cname, coll in (state.get("collections") or {}).items():
        members = list((coll or {}).get("members") or [])
        items.append(
            {
                "kind": "collection",
                "ref": str(cname),
                "payload": {"member_count": len(members)},
                "source_turn_id": "",
                "status": "active",
            }
        )
    return items
