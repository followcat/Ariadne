from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

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

MAX_ENTITIES = 256
MAX_RELATIONS_PER_TYPE = 64
MAX_RELATION_TYPES = 32
MAX_COLLECTION_MEMBERS = 64
MAX_COLLECTIONS = 32


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "entities": {}, "relations": {}, "collections": {}}


@dataclass
class ConversationStateStore:
    """Authoritative L2 state. File is fcntl-locked for multi-process safety."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"documents": {}})

    @property
    def store_identity(self) -> str:
        """Stable opaque identity used to fence cross-store journal replay."""

        digest = hashlib.sha256(
            str(self.path.resolve()).encode("utf-8")
        ).hexdigest()
        return f"conversation-state-v1:{digest}"

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(self.path, default={"documents": {}})
        return data if isinstance(data, dict) else {"documents": {}}

    def _write(self, data: dict[str, Any]) -> None:
        locked_write_json(self.path, data)

    def get(self, session_id: str) -> dict[str, Any]:
        data = self._read()
        doc = (data.get("documents") or {}).get(session_id)
        if not doc:
            return empty_state()
        return dict(doc.get("state") or empty_state())

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
        """Resolve a host-owned task_id to an immutable goal entity id."""

        tid = (task_id or "").strip()
        if not tid:
            return None
        entities = self.get(session_id).get("entities") or {}
        matches: list[str] = []
        for entity_id, entity in entities.items():
            if not isinstance(entity, dict) or entity.get("type") != "goal":
                continue
            payload = (entity.get("attributes") or {}).get("task_id")
            if not isinstance(payload, dict) or payload.get("status") != "active":
                continue
            if str(payload.get("value") or "") == tid:
                matches.append(str(entity_id))
        if not matches:
            return None
        matches.sort()
        return matches[0]

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

    def version(self, session_id: str) -> int:
        """Current document version (0 when never projected)."""
        data = self._read()
        doc = (data.get("documents") or {}).get(session_id)
        if not doc:
            return 0
        return int(doc.get("version") or 0)

    def list_versions(self, session_id: str) -> list[dict[str, Any]]:
        """Append-only projection history for the session."""
        data = self._read()
        return list((data.get("versions") or {}).get(session_id) or [])

    def watermark(self, session_id: str) -> str | None:
        """Turn id of the last succeeded projection, or None if never projected."""
        data = self._read()
        doc = (data.get("documents") or {}).get(session_id)
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
        if len(text) > 8000:
            raise AriadneError(app_error("ARIADNE_MEMORY_NOT_READY", "state render exceeds hard cap"))
        return text, len(entities)

    def apply_ops(
        self,
        *,
        session_id: str,
        operations: list[dict[str, Any]],
        source_turn_id: str,
        evidence_text: str,
        expected_parent_version: int | None = None,
        idempotency_key: str | None = None,
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
            }[name]
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
            if name in {"set_attribute", "expire_attribute", "set_current_goal"}:
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

        result_holder: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            scoped_key = (
                f"{session_id}:{idempotency_key}"
                if idempotency_key is not None
                else ""
            )
            applied = data.setdefault("idempotency_keys", {})
            if scoped_key and scoped_key in applied:
                result_holder.update(dict(applied[scoped_key]))
                result_holder["idempotent_replay"] = True
                return data
            docs = data.setdefault("documents", {})
            doc = docs.get(session_id) or {}
            current_version = int(doc.get("version") or 0)
            if expected_parent_version is not None and expected_parent_version != current_version:
                raise AriadneError(
                    app_error(
                        "ARIADNE_MEMORY_NOT_READY",
                        "state version conflict (CAS parent mismatch)",
                        expected_parent_version=expected_parent_version,
                        current_version=current_version,
                    )
                )
            state = copy.deepcopy(doc.get("state") or empty_state())
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
            new_version = current_version + 1
            docs[session_id] = {
                "state": state,
                "watermark_turn_id": source_turn_id,
                "version": new_version,
            }
            versions = data.setdefault("versions", {}).setdefault(session_id, [])
            versions.append(
                {
                    "version": new_version,
                    "parent_version": current_version,
                    "watermark_turn_id": source_turn_id,
                    "source_turn_id": source_turn_id,
                    "ops": [str(op.get("op")) for op in operations],
                    "operations": copy.deepcopy(operations),
                }
            )
            result_holder.update(
                {
                    "decision": "apply",
                    "state": state,
                    "ops": len(operations),
                    "version": new_version,
                    "parent_version": current_version,
                }
            )
            if scoped_key:
                applied[scoped_key] = {
                    "decision": "apply",
                    "ops": len(operations),
                    "version": new_version,
                    "parent_version": current_version,
                }
            return data

        locked_update_json(self.path, mut, default={"documents": {}})
        return result_holder

    def _apply_one(self, state: dict[str, Any], op: dict[str, Any], *, source_turn_id: str) -> None:
        entities: dict[str, Any] = state.setdefault("entities", {})
        collections: dict[str, Any] = state.setdefault("collections", {})
        relations: dict[str, Any] = state.setdefault("relations", {})
        name = op["op"]
        if name == "set_current_goal":
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
                legacy_goal_id = (
                    "goal:legacy:" + hashlib.sha256(encoded).hexdigest()[:20]
                )
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
