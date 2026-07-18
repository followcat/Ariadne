"""Local web users with BYOK provider binding.

Personal self-hosted model: users register with username/password
(pbkdf2 hash), get a bearer token, and bind their own LLM provider
(BASE_URL / API_KEY / MODEL). Store is a single JSON file, mode 0600.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

_PBKDF2_ITERATIONS = 120_000


def _hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return digest.hex()


@dataclass
class UserStore:
    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"users": {}})

    def _read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.path, 0o600)

    def register(self, username: str, password: str) -> str:
        username = username.strip()
        if not username or len(username) > 64:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "invalid username"))
        if len(password) < 8:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "password must be at least 8 chars")
            )
        data = self._read()
        users = data.setdefault("users", {})
        if username in users:
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", f"user exists: {username}")
            )
        salt = secrets.token_hex(16)
        token = secrets.token_hex(24)
        users[username] = {
            "salt": salt,
            "password_hash": _hash_password(password, salt),
            "token": token,
            "provider": {},
        }
        self._write(data)
        return token

    def login(self, username: str, password: str) -> str:
        data = self._read()
        user = (data.get("users") or {}).get(username.strip())
        if user is None or not secrets.compare_digest(
            user["password_hash"], _hash_password(password, user["salt"])
        ):
            raise AriadneError(
                app_error("ARIADNE_TOOL_DENIED", "invalid username or password")
            )
        return str(user["token"])

    def username_for_token(self, token: str) -> str | None:
        data = self._read()
        for username, user in (data.get("users") or {}).items():
            if secrets.compare_digest(str(user.get("token") or ""), token):
                return username
        return None

    def set_provider(self, username: str, *, base_url: str, api_key: str, model: str) -> None:
        if not base_url.strip() or not api_key.strip() or not model.strip():
            raise AriadneError(
                app_error("ARIADNE_INVALID_TOOL_ARGS", "base_url, api_key and model are required")
            )
        data = self._read()
        user = (data.get("users") or {}).get(username)
        if user is None:
            raise AriadneError(app_error("ARIADNE_TOOL_DENIED", "unknown user"))
        user["provider"] = {
            "base_url": base_url.strip().rstrip("/"),
            "api_key": api_key.strip(),
            "model": model.strip(),
        }
        self._write(data)

    def get_provider(self, username: str) -> dict[str, str]:
        data = self._read()
        user = (data.get("users") or {}).get(username)
        if user is None:
            raise AriadneError(app_error("ARIADNE_TOOL_DENIED", "unknown user"))
        return dict(user.get("provider") or {})
