#!/usr/bin/env python3
"""Minimal LLM smoke test using Ariadne .env (OpenAI-compatible).

Usage:
  env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      python3 scripts/llm_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.is_file():
        raise SystemExit(f"missing env file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        cfg[key.strip()] = value.strip().strip('"').strip("'")
    return cfg


def main() -> int:
    for key in list(os.environ):
        if "proxy" in key.lower():
            os.environ.pop(key, None)

    root = Path(__file__).resolve().parents[1]
    cfg = load_env(root / ".env")
    base = (cfg.get("BASE_URL") or "").rstrip("/")
    api_key = cfg.get("API_KEY") or ""
    model = cfg.get("MODEL") or ""
    if not base or not api_key:
        print("SMOKE_FAIL: BASE_URL and API_KEY are required in .env", file=sys.stderr)
        return 2
    if not model:
        print("SMOKE_FAIL: MODEL is required in .env", file=sys.stderr)
        return 2

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply with exactly: ARIADNE_OK"},
        ],
        "temperature": 0,
        "max_tokens": 16,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Ariadne-llm-smoke/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        print(f"SMOKE_FAIL: HTTP {exc.code}", file=sys.stderr)
        print(err[:300].replace("\n", " "), file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001 - smoke script surface
        print(f"SMOKE_FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    obj = json.loads(body)
    content = (((obj.get("choices") or [{}])[0].get("message") or {}).get("content"))
    print(f"status={status}")
    print(f"model={model}")
    print(f"content={content!r}")
    print(f"usage={obj.get('usage')}")
    if "ARIADNE_OK" not in str(content or ""):
        print("SMOKE_FAIL: unexpected content", file=sys.stderr)
        return 5
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
