"""Bounded conversation-state working set assembly."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..errors import AriadneError, app_error

_LATIN_TERM = re.compile(r"[A-Za-z0-9_.:-]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")
_SELECT_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "is",
        "of",
        "the",
        "to",
        "what",
        "which",
        "with",
        "的",
        "了",
        "吗",
        "呢",
        "吧",
        "啊",
        "是",
        "在",
        "有",
        "和",
        "与",
        "哪",
        "什么",
        "怎么",
        "现在",
    }
)

WORKING_SET_HEADER_COMPLETE = (
    "[CONVERSATION_STATE_WORKING_SET]\n"
    "Authority: complete current typed projection."
)
WORKING_SET_HEADER_SELECTED = (
    "[CONVERSATION_STATE_WORKING_SET]\n"
    "Authority: current typed projection. This is a non-exhaustive working set; "
    "use conversation_state_lookup for omitted current facts."
)
COLLECTION_LOOKUP_HINT = (
    "members omitted from working set; use conversation_state_lookup with the collection ref"
)
COMPLETE_ROW_LIMIT = 50


@dataclass(frozen=True, slots=True)
class WorkingSetResult:
    text: str
    char_count: int
    selected_count: int
    omitted_count: int
    selection_mode: str
    projection_seq: int
    state_json: dict[str, Any]


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(str(cursor).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AriadneError(
            app_error("ARIADNE_INVALID_TOOL_ARGS", "conversation_state_lookup cursor is invalid")
        ) from exc
    if not isinstance(payload, dict):
        raise AriadneError(
            app_error("ARIADNE_INVALID_TOOL_ARGS", "conversation_state_lookup cursor is invalid")
        )
    return payload


def query_hash(query: str) -> str:
    return hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]


def render_item(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "")
    ref = str(item.get("ref") or "")
    payload = item.get("payload") or {}
    if kind == "entity":
        aliases = ", ".join(payload.get("aliases") or []) or "-"
        return (
            f"- {ref} ({payload.get('type') or 'generic'}) "
            f"status={payload.get('status') or 'active'} "
            f"status_authority={payload.get('status_authority') or 'model_inferred'} "
            f"aliases=[{aliases}]"
        )
    if kind == "fact":
        return (
            f"    {payload.get('entity_id')}.{payload.get('key')}="
            f"{payload.get('value')!r} "
            f"status={item.get('status') or 'active'} "
            f"type={payload.get('memory_type') or 'fact'} "
            f"authority={payload.get('authority') or 'model_inferred'}"
        )
    if kind == "relation":
        return (
            f"- relation {payload.get('relation')}: "
            f"{payload.get('from')} -> {payload.get('to')}"
        )
    if kind == "collection":
        count = int(payload.get("member_count") or 0)
        line = f"- collection {ref}: member_count={count}"
        if count:
            line += f"\n  {COLLECTION_LOOKUP_HINT}"
        return line
    return f"- {kind} {ref}"


def assemble_working_set(
    items: list[dict[str, Any]],
    *,
    query: str,
    projection_seq: int,
    soft_chars: int,
    hard_chars: int,
    member_count: int = 0,
) -> WorkingSetResult:
    if hard_chars < 256:
        raise AriadneError(
            app_error("ARIADNE_CONFIG_INVALID", "working_set_hard_chars must be at least 256")
        )
    target = min(max(int(soft_chars), 256), int(hard_chars))
    visible = [item for item in items if str(item.get("status") or "active") not in {"superseded", "expired"}]
    complete_ok = (len(visible) + int(member_count)) <= COMPLETE_ROW_LIMIT
    complete_blocks = [render_item(item) for item in visible]
    complete_text = WORKING_SET_HEADER_COMPLETE + (
        "\n" + "\n".join(complete_blocks) if complete_blocks else "\n(empty projection)"
    )
    if visible and complete_ok and len(complete_text) <= target:
        selected = visible
        header = WORKING_SET_HEADER_COMPLETE
        mode = "complete"
        blocks = complete_blocks
    elif not visible:
        selected = []
        header = WORKING_SET_HEADER_COMPLETE
        mode = "complete"
        blocks = []
    else:
        selected = _select_items(visible, query)
        header = WORKING_SET_HEADER_SELECTED
        mode = "selected"
        blocks = []
        kept: list[dict[str, Any]] = []
        for item in selected:
            block = render_item(item)
            candidate = header + "\n" + "\n".join([*blocks, block])
            if len(candidate) > target:
                continue
            kept.append(item)
            blocks.append(block)
        selected = kept
    text = header + ("\n" + "\n".join(blocks) if blocks else "\n(empty selection)")
    if len(text) > hard_chars:
        raise AriadneError(
            app_error(
                "ARIADNE_MEMORY_WORKING_SET_OVERFLOW",
                "working-set assembler exceeded its hard character contract",
                char_count=len(text),
                hard_chars=hard_chars,
            )
        )
    return WorkingSetResult(
        text=text,
        char_count=len(text),
        selected_count=len(selected),
        omitted_count=max(len(visible) + member_count - len(selected), 0),
        selection_mode=mode,
        projection_seq=projection_seq,
        state_json=_items_to_partial_state(selected),
    )


def lexical_terms(query: str) -> list[str]:
    """Split a query into latin/ref tokens plus CJK unigrams and bigrams."""

    text = (query or "").strip()
    if not text:
        return []
    extras: list[str] = list(_LATIN_TERM.findall(text))
    compact = re.sub(r"\s+", "", text)
    if _CJK_CHAR.search(compact):
        cjk = _CJK_CHAR.findall(compact)
        extras.extend(cjk)
        extras.extend("".join(cjk[i : i + 2]) for i in range(len(cjk) - 1))
    seen: set[str] = set()
    out: list[str] = []
    for term in extras:
        key = term.lower()
        if not term or key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _select_items(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    if not q:
        return list(items)
    tokens = [
        token.lower()
        for token in lexical_terms(query)
        if token.lower() not in _SELECT_STOP
    ]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        ref = str(item.get("ref") or "").lower()
        blob = " ".join(
            [ref, json.dumps(item.get("payload") or {}, ensure_ascii=False)]
        ).lower()
        score = 0
        if ref == q or ref in tokens:
            score += 100
        for token in tokens:
            if token == ref or ref.startswith(f"{token}.") or ref.endswith(f".{token}"):
                score += 50
            elif token in blob:
                score += 5
        if score:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("kind")), str(pair[1].get("ref"))))
    if not scored:
        return []
    best = scored[0][0]
    picked = [item for score, item in scored if score >= max(best // 2, 50)][:32]
    if not picked:
        picked = [item for _score, item in scored[:16]]
    return _with_entity_closure(items, picked)


def _with_entity_closure(
    all_items: list[dict[str, Any]], picked: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    needed: set[str] = set()
    for item in picked:
        payload = item.get("payload") or {}
        kind = str(item.get("kind") or "")
        if kind == "fact":
            needed.add(str(payload.get("entity_id") or ""))
        elif kind == "relation":
            needed.add(str(payload.get("from") or ""))
            needed.add(str(payload.get("to") or ""))
        elif kind == "entity":
            needed.add(str(item.get("ref") or ""))
    extra = [
        item
        for item in all_items
        if item not in picked
        and (
            (
                str(item.get("kind") or "") == "entity"
                and str(item.get("ref") or "") in needed
            )
            or (
                str(item.get("kind") or "") == "fact"
                and str((item.get("payload") or {}).get("entity_id") or "") in needed
            )
        )
    ]
    return [*extra, *picked]


def _items_to_partial_state(items: list[dict[str, Any]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": 1,
        "entities": {},
        "relations": {},
        "collections": {},
    }
    for item in items:
        kind = str(item.get("kind") or "")
        payload = item.get("payload") or {}
        if kind == "entity":
            state["entities"][str(item.get("ref"))] = {
                "type": payload.get("type") or "generic",
                "aliases": list(payload.get("aliases") or []),
                "attributes": {},
                "status": payload.get("status") or "active",
                "status_authority": payload.get("status_authority") or "model_inferred",
            }
        elif kind == "fact":
            eid = str(payload.get("entity_id") or "")
            ent = state["entities"].setdefault(
                eid,
                {"type": "generic", "aliases": [], "attributes": {}, "status": "active"},
            )
            ent.setdefault("attributes", {})[str(payload.get("key"))] = {
                "value": payload.get("value"),
                "authority": payload.get("authority"),
                "memory_type": payload.get("memory_type"),
                "status": item.get("status") or "active",
            }
        elif kind == "relation":
            name = str(payload.get("relation") or "")
            state["relations"].setdefault(name, []).append(
                {"from": payload.get("from"), "to": payload.get("to")}
            )
        elif kind == "collection":
            state["collections"][str(item.get("ref"))] = {
                "member_count": payload.get("member_count") or 0
            }
    return state
