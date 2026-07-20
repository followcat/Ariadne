"""Cross-process safe JSON file read/write for shared memory stores.

Agent turns and ``spawn_worker_process`` may touch the same
``summaries.json`` / ``projection_jobs.json`` / ``state.json``. Without
locking, concurrent read-modify-write loses updates.

Strategy:

- Sidecar ``*.lock`` file + ``fcntl.flock`` (exclusive for RMW/write,
  shared for pure reads).
- Atomic replace via temp file + ``os.replace`` while holding the lock.

In-process and sub-process workers may run together; they must all use
these helpers for the shared paths.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def locked_read_json(path: Path, *, default: Any) -> Any:
    """Read JSON under a shared lock. Missing file → default (not written)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_SH)
        try:
            if not path.exists():
                return default
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return default
            return json.loads(raw)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def locked_write_json(path: Path, data: Any) -> None:
    """Write JSON under an exclusive lock (atomic replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def locked_update_json(
    path: Path,
    mutator: Callable[[Any], Any],
    *,
    default: Any,
) -> Any:
    """Exclusive RMW: load → mutator(data) → write. Returns final data.

    ``mutator`` receives a deep-ish working object (parsed JSON) and must
    return the object to persist (may mutate in place and return it).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(path)
    with lock.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else default
            else:
                data = default
            # mutator may mutate; ensure we have a dict/list root
            if data is None:
                data = default
            out = mutator(data)
            if out is None:
                out = data
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
            return out
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
