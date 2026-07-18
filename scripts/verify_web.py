"""Playwright end-to-end verification of the Ariadne web UI.

Starts `ariadne serve` on a scratch workspace, then drives a browser:
register -> provider binding (BYOK) -> send a turn -> streamed reply.

Usage: PYTHONPATH=src python3 scripts/verify_web.py
Requires: playwright + chromium, and a working provider in .env.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = 8471


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    workspace = Path(tempfile.mkdtemp(prefix="ariadne-web-e2e-"))
    env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}
    env["PYTHONPATH"] = str(repo / "src")

    server = subprocess.Popen(
        [
            sys.executable, "-m", "ariadne",
            "--workspace", str(workspace),
            "serve", "--host", "127.0.0.1", "--port", str(PORT),
        ],
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(60):
            try:
                import urllib.request
                import urllib.error

                try:
                    urllib.request.urlopen(base + "/api/me", timeout=1)
                except urllib.error.HTTPError as http_err:
                    if http_err.code in {400, 401, 405}:
                        break  # server is up, endpoint just needs auth
                    raise
                else:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            print("server did not start")
            return 1

        # provider credentials come from the repo .env
        provider: dict[str, str] = {}
        for line in (repo / ".env").read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                provider[k.strip()] = v.strip().strip('"').strip("'")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(base)
            # register
            page.fill("#u", "e2e-user")
            page.fill("#p", "e2e-password-123")
            page.click("#register")
            page.wait_for_selector("#app:not(.hidden)", timeout=10_000)
            print("ok: registered and entered app")
            # bind provider (BYOK)
            page.fill("#baseurl", provider["BASE_URL"])
            page.fill("#apikey", provider["API_KEY"])
            page.fill("#model", provider.get("MODEL", "kimi-k2.7-code"))
            page.click("#saveprovider")
            page.wait_for_selector("#settings.hidden", state="attached", timeout=10_000)
            print("ok: provider bound")
            # send a turn and wait for a streamed reply
            page.fill("#input", "Reply with exactly one word: webtest")
            page.click("#send")
            page.wait_for_function(
                """() => {
                    const msgs = document.querySelectorAll('.msg.assistant');
                    if (!msgs.length) return false;
                    const t = msgs[msgs.length-1].textContent;
                    return t && t !== '…' && t.length > 0;
                }""",
                timeout=120_000,
            )
            reply = page.eval_on_selector_all(
                ".msg.assistant", "els => els[els.length-1].textContent"
            )
            assert "webtest" in reply.lower(), f"unexpected reply: {reply!r}"
            print(f"ok: streamed reply: {reply!r}")
            # reload: token persists, still logged in
            page.reload()
            page.wait_for_selector("#app:not(.hidden)", timeout=10_000)
            print("ok: session persists across reload")
            browser.close()
        print("WEB E2E PASS")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
