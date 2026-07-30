from __future__ import annotations

import hashlib
from pathlib import Path

_IGNORED_DIRS = frozenset({".git", ".ariadne", ".venv", "node_modules", "__pycache__"})


def workspace_fingerprint(workspace: Path | None, *, max_files: int = 10_000) -> str:
    """Return a bounded change detector. It is deliberately not completion evidence."""
    if workspace is None:
        return "workspace:none"
    root = Path(workspace).resolve()
    if not root.is_dir():
        return "workspace:missing"
    digest = hashlib.sha256()
    seen = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _IGNORED_DIRS for part in rel.parts):
            continue
        if path.is_symlink():
            digest.update(f"L\0{rel.as_posix()}\0{path.readlink()}\n".encode())
            continue
        if not path.is_file():
            continue
        seen += 1
        if seen > max_files:
            digest.update(f"OVERFLOW\0{seen}\n".encode())
            break
        stat = path.stat()
        digest.update(f"F\0{rel.as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return f"tree-v1:{digest.hexdigest()}:{min(seen, max_files)}"
