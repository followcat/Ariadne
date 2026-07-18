from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

ALLOWED_OPS = {
    "ensure_entity",
    "set_alias",
    "set_attribute",
    "set_status",
    "ensure_collection",
    "collection_append",
    "collection_remove",
}

MAX_ENTITIES = 256


def empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "entities": {}, "relations": {}, "collections": {}}


@dataclass
class ConversationStateStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"documents": {}})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get(self, session_id: str) -> dict[str, Any]:
        data = self._read()
        doc = (data.get("documents") or {}).get(session_id)
        if not doc:
            return empty_state()
        return dict(doc.get("state") or empty_state())

    def watermark(self, session_id: str) -> str | None:
        """Turn id of the last succeeded projection, or None if never projected."""
        data = self._read()
        doc = (data.get("documents") or {}).get(session_id)
        if not doc:
            return None
        wm = doc.get("watermark_turn_id")
        return str(wm) if wm else None

    def render(self, session_id: str) -> tuple[str, int]:
        state = self.get(session_id)
        entities = state.get("entities") or {}
        if not entities and not (state.get("collections") or {}):
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
    ) -> dict[str, Any]:
        if not operations:
            return {"decision": "no_change", "state": self.get(session_id)}
        state = self.get(session_id)
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
            self._apply_one(state, op, source_turn_id=source_turn_id)
        if len(state.get("entities") or {}) > MAX_ENTITIES:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "entity capacity exceeded"))
        data = self._read()
        docs = data.setdefault("documents", {})
        docs[session_id] = {
            "state": state,
            "watermark_turn_id": source_turn_id,
        }
        self._write(data)
        return {"decision": "apply", "state": state, "ops": len(operations)}

    def _apply_one(self, state: dict[str, Any], op: dict[str, Any], *, source_turn_id: str) -> None:
        entities: dict[str, Any] = state.setdefault("entities", {})
        collections: dict[str, Any] = state.setdefault("collections", {})
        name = op["op"]
        if name == "ensure_entity":
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
