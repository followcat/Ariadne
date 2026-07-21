"""Persistent tool approval grants (host concern).

Stores pending → approved | denied | expired | executed records under the
host data dir so on-request approvals survive process restarts.

Kernel still only sees a boolean approval_hook; this module is the durable
state behind that hook.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


STATUSES = frozenset({"pending", "approved", "denied", "expired", "executed"})


def fingerprint(name: str, args: dict[str, Any]) -> str:
    """Stable hash of tool name + canonical args."""
    payload = json.dumps(
        {"name": name, "args": args or {}},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class GrantStore:
    path: Path
    default_ttl_seconds: float = 3600.0

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [r for r in raw if isinstance(r, dict)]

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = self._read()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    def get(self, grant_id: str) -> dict[str, Any] | None:
        for r in self._read():
            if r.get("id") == grant_id:
                return r
        return None

    def create_pending(
        self,
        *,
        name: str,
        args: dict[str, Any],
        session_id: str = "",
        ttl_seconds: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        ttl = self.default_ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        row = {
            "id": str(uuid.uuid4()),
            "name": name,
            "args": args or {},
            "fingerprint": fingerprint(name, args or {}),
            "session_id": session_id or "",
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + ttl,
        }
        rows = self._read()
        rows.append(row)
        self._write(rows)
        return row

    def _update(self, grant_id: str, **fields: Any) -> dict[str, Any] | None:
        rows = self._read()
        out: dict[str, Any] | None = None
        for r in rows:
            if r.get("id") == grant_id:
                r.update(fields)
                r["updated_at"] = time.time()
                out = r
                break
        if out is not None:
            self._write(rows)
        return out

    def approve(self, grant_id: str) -> dict[str, Any] | None:
        g = self.get(grant_id)
        if g is None:
            return None
        if g.get("status") not in {"pending", "approved"}:
            return g
        return self._update(grant_id, status="approved")

    def deny(self, grant_id: str) -> dict[str, Any] | None:
        g = self.get(grant_id)
        if g is None:
            return None
        if g.get("status") not in {"pending", "approved"}:
            return g
        return self._update(grant_id, status="denied")

    def mark_executed(self, grant_id: str) -> dict[str, Any] | None:
        g = self.get(grant_id)
        if g is None:
            return None
        if g.get("status") not in {"approved", "pending", "executed"}:
            return g
        return self._update(grant_id, status="executed")

    def expire_due(self, *, now: float | None = None) -> int:
        """Mark pending/approved past expires_at as expired. Returns count."""
        ts = time.time() if now is None else now
        rows = self._read()
        n = 0
        for r in rows:
            if r.get("status") in {"pending", "approved", "executed"} and float(
                r.get("expires_at") or 0
            ) < ts:
                r["status"] = "expired"
                r["updated_at"] = ts
                n += 1
        if n:
            self._write(rows)
        return n

    def find_usable(
        self,
        name: str,
        args: dict[str, Any],
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """Return a non-expired grant matching fingerprint that may run the tool.

        Accepts ``approved`` (user said yes) and ``executed`` (already ran once):
        both remain reusable until TTL or explicit deny — otherwise a restart
        after the first successful on-request approval would re-prompt forever.
        """
        self.expire_due(now=now)
        ts = time.time() if now is None else now
        fp = fingerprint(name, args or {})
        for r in self._read():
            if r.get("fingerprint") != fp:
                continue
            if r.get("status") not in {"approved", "executed"}:
                continue
            if float(r.get("expires_at") or 0) < ts:
                continue
            return r
        return None
