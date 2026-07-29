"""Chat markdown: mermaid/svg fences become renderable containers (not plain code)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
SCRIPT = FRONTEND / "scripts" / "check-markdown-diagrams.mjs"


def test_mermaid_and_svg_fences_become_diagram_containers() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    node = shutil.which("node")
    assert node, "node required for frontend markdown diagram check"
    # tsx resolves .ts imports without vite file-watchers (ENOSPC-safe).
    tsx = FRONTEND / "node_modules" / "tsx" / "dist" / "cli.mjs"
    if tsx.is_file():
        cmd = [node, "--import", "tsx", str(SCRIPT)]
    else:
        cmd = [node, "--import", "tsx", str(SCRIPT)]
    proc = subprocess.run(
        cmd,
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        timeout=60,
        env={**dict(**__import__("os").environ), "NODE_NO_WARNINGS": "1"},
    )
    assert proc.returncode == 0, (
        f"diagram check failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "OK" in proc.stdout
