from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error
from .json_file import locked_read_json, locked_update_json, locked_write_json

ALLOWED_OPS = {
    "ensure_entity",
    "set_alias",
    "set_attribute",
    "set_status",
    "set_relation",
    "remove_relation",
    "ensure_collection",
    "collection_append",
    "collection_remove",
    "collection_move",
}

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

    def get_as_of(
        self, session_id: str, *, allowed_turn_ids: set[str] | None = None
    ) -> dict[str, Any]:
        """Point-in-time state: drop attributes sourced after the cutoff turns.

        When ``allowed_turn_ids`` is None, returns current state. Attributes
        without ``source_turn_id`` are kept (conservative). Entities that end
        up with no attributes remain if they still have aliases or type.
        """
        state = self.get(session_id)
        if allowed_turn_ids is None:
            return state
        filtered = copy.deepcopy(state)
        entities = filtered.get("entities") or {}
        for _eid, ent in list(entities.items()):
            attrs = ent.get("attributes") or {}
            kept: dict[str, Any] = {}
            for key, payload in attrs.items():
                if isinstance(payload, dict):
                    src = str(payload.get("source_turn_id") or "").strip()
                    if src and src not in allowed_turn_ids:
                        continue
                kept[key] = payload
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
            lines.append(f"- {eid} ({ent.get('type') or 'generic'}) status={status} aliases=[{aliases}]")
            attrs = ent.get("attributes") or {}
            for key, payload in attrs.items():
                if isinstance(payload, dict):
                    lines.append(f"    {key}={payload.get('value')!r} source_turn={payload.get('source_turn_id')}")
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
    ) -> dict[str, Any]:
        if not operations:
            return {"decision": "no_change", "state": self.get(session_id)}
        # Validate ops against evidence before locking (no invent / no bad quotes).
        evidence = evidence_text or ""
        for op in operations:
            name = str(op.get("op") or "").strip()
            if name not in ALLOWED_OPS:
                raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", f"unknown state op: {name}"))
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
                    "ops": [str(op.get("op")) for op in operations],
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
            return data

        locked_update_json(self.path, mut, default={"documents": {}})
        return result_holder

    def _apply_one(self, state: dict[str, Any], op: dict[str, Any], *, source_turn_id: str) -> None:
        entities: dict[str, Any] = state.setdefault("entities", {})
        collections: dict[str, Any] = state.setdefault("collections", {})
        relations: dict[str, Any] = state.setdefault("relations", {})
        name = op["op"]
        if name == "set_relation":
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
            ent.setdefault("attributes", {})[key] = {
                "value": op.get("value"),
                "source_turn_id": source_turn_id,
                "authority": str(op.get("authority") or "user_explicit"),
            }
        elif name == "set_status":
            eid = str(op["entity_id"])
            ent = entities.setdefault(
                eid, {"type": "generic", "aliases": [], "attributes": {}, "status": "active"}
            )
            ent["status"] = str(op.get("status") or "active")
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
