from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _strip(value: str) -> str:
    return value.strip().strip('"').strip("'")


def load_dotenv(path: Path) -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not path.is_file():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        cfg[key.strip()] = _strip(value)
    return cfg


def find_dotenv(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    for directory in [cur, *cur.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
        if directory == directory.parent:
            break
    return None


def find_dotenv_candidates(*starts: Path | None) -> Path | None:
    seen: set[Path] = set()
    for start in starts:
        if start is None:
            continue
        path = find_dotenv(start)
        if path is not None and path not in seen:
            return path
        seen.add((start or Path.cwd()).resolve())
    # package repo root (src/ariadne/config.py -> parents[2] == repo root when installed as src layout)
    here = Path(__file__).resolve()
    for directory in here.parents:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


@dataclass(slots=True)
class Settings:
    base_url: str
    api_key: str
    model: str
    workspace: Path
    session_id: str
    sandbox: str = "local"  # local | null
    tool_loop_limit: int = 16
    verbose: bool = False
    json_mode: bool = False
    data_dir: Path | None = None
    skills_dir: Path | None = None
    prefer_deferred_tools: bool = True

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir
        return self.workspace / ".ariadne"


def load_settings(
    *,
    workspace: Path | None = None,
    session_id: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    tool_loop_limit: int | None = None,
    verbose: bool = False,
    json_mode: bool = False,
    env_file: Path | None = None,
    skills_dir: Path | None = None,
    prefer_deferred_tools: bool | None = None,
) -> Settings:
    workspace = (workspace or Path.cwd()).resolve()
    file_cfg: dict[str, str] = {}
    dotenv = env_file or find_dotenv_candidates(workspace, Path.cwd())
    if dotenv is not None:
        file_cfg = load_dotenv(dotenv)

    def pick(*keys: str, default: str = "") -> str:
        for key in keys:
            if os.environ.get(key):
                return os.environ[key].strip()
            if file_cfg.get(key):
                return file_cfg[key]
        return default

    base_url = pick("BASE_URL", "OPENAI_BASE_URL").rstrip("/")
    api_key = pick("API_KEY", "OPENAI_API_KEY")
    model_name = model or pick("MODEL", "OPENAI_MODEL", default="grok-4.5")
    sandbox_name = (sandbox or pick("ARIADNE_SANDBOX", default="local")).strip().lower()
    sid = session_id or pick("ARIADNE_SESSION", default="default")
    limit = tool_loop_limit
    if limit is None:
        raw = pick("ARIADNE_TOOL_LOOP_LIMIT", default="16")
        limit = max(int(raw or 16), 1)

    return Settings(
        base_url=base_url,
        api_key=api_key,
        model=model_name,
        workspace=workspace,
        session_id=sid,
        sandbox=sandbox_name,
        tool_loop_limit=limit,
        verbose=verbose,
        json_mode=json_mode,
        skills_dir=skills_dir,
        prefer_deferred_tools=True if prefer_deferred_tools is None else prefer_deferred_tools,
    )
