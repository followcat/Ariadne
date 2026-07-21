"""Atelier persistence: Project + Session meta + paths."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..errors import AriadneError, app_error

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class SessionType(str, Enum):
    MAIN = "main"
    BRANCH = "branch"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    MERGED = "merged"
    DISCARDED = "discarded"


def default_atelier_root() -> Path:
    import os

    raw = os.environ.get("ARIADNE_ATELIER_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".ariadne" / "ateliers").resolve()


def validate_slug(name: str) -> str:
    slug = (name or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"invalid atelier id {name!r} (use [a-z0-9][a-z0-9._-]{{0,63}})",
            )
        )
    return slug


@dataclass
class ProjectConfig:
    sandbox_profile: str = "minimal"
    docker_image: str | None = None
    network_mode: str = "none"
    skills_dirs: list[str] = field(default_factory=list)
    env_inject: dict[str, str] = field(default_factory=dict)
    max_tool_loop: int = 32

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_profile": self.sandbox_profile,
            "docker_image": self.docker_image,
            "network_mode": self.network_mode,
            "skills_dirs": list(self.skills_dirs),
            "env_inject": dict(self.env_inject),
            "max_tool_loop": int(self.max_tool_loop),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ProjectConfig:
        d = data or {}
        return cls(
            sandbox_profile=str(d.get("sandbox_profile") or "minimal"),
            docker_image=d.get("docker_image"),
            network_mode=str(d.get("network_mode") or "none"),
            skills_dirs=[str(x) for x in (d.get("skills_dirs") or [])],
            env_inject={str(k): str(v) for k, v in (d.get("env_inject") or {}).items()},
            max_tool_loop=int(d.get("max_tool_loop") or 32),
        )


@dataclass
class Project:
    id: str
    name: str
    path: Path
    workspace_path: Path
    config: ProjectConfig = field(default_factory=ProjectConfig)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def knowledge_path(self) -> Path:
        return self.path / "KNOWLEDGE.md"

    @property
    def sessions_dir(self) -> Path:
        return self.path / ".ariadne" / "sessions"

    @property
    def knowledge_history_dir(self) -> Path:
        return self.path / ".ariadne" / "knowledge_history"

    @property
    def data_dir(self) -> Path:
        """Per-atelier data dir for sandbox/memory under project."""
        return self.path / ".ariadne"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": str(self.path),
            "workspace_path": str(self.workspace_path),
            "config": self.config.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Project:
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            path=Path(data["path"]),
            workspace_path=Path(data["workspace_path"]),
            config=ProjectConfig.from_dict(data.get("config") if isinstance(data.get("config"), dict) else {}),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def save(self) -> None:
        self.updated_at = time.time()
        self.path.mkdir(parents=True, exist_ok=True)
        path = self.path / "project.yaml"
        # YAML-ish simple dump (no PyYAML dependency)
        cfg = self.config.to_dict()
        lines = [
            f"id: {self.id}",
            f"name: {json.dumps(self.name, ensure_ascii=False)}",
            f"path: {json.dumps(str(self.path))}",
            f"workspace_path: {json.dumps(str(self.workspace_path))}",
            f"created_at: {self.created_at}",
            f"updated_at: {self.updated_at}",
            "config:",
            f"  sandbox_profile: {cfg['sandbox_profile']}",
            f"  docker_image: {cfg['docker_image']!r}" if cfg["docker_image"] else "  docker_image: null",
            f"  network_mode: {cfg['network_mode']}",
            f"  max_tool_loop: {cfg['max_tool_loop']}",
            "  skills_dirs: " + json.dumps(cfg["skills_dirs"]),
            "  env_inject: " + json.dumps(cfg["env_inject"], ensure_ascii=False),
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")

    @classmethod
    def load(cls, project_dir: Path) -> Project:
        path = project_dir / "project.yaml"
        if not path.is_file():
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", f"not an atelier: {project_dir}")
            )
        # Prefer companion JSON if present (round-trip friendly)
        jpath = project_dir / "project.json"
        if jpath.is_file():
            data = json.loads(jpath.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        # Minimal yaml parse via JSON companion write on save — also write project.json
        text = path.read_text(encoding="utf-8")
        data: dict[str, Any] = {"config": {}}
        config_mode = False
        for line in text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            if line.startswith("config:"):
                config_mode = True
                continue
            if config_mode and line.startswith("  "):
                if ":" not in line:
                    continue
                k, v = line.strip().split(":", 1)
                k, v = k.strip(), v.strip()
                if v in {"null", "None", ""}:
                    data["config"][k] = None
                elif v.startswith("[") or v.startswith("{"):
                    data["config"][k] = json.loads(v)
                elif v.replace(".", "", 1).isdigit():
                    data["config"][k] = float(v) if "." in v else int(v)
                else:
                    data["config"][k] = v.strip("'\"")
                continue
            config_mode = False
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip()
            if v.startswith('"') or v.startswith("'"):
                data[k] = json.loads(v.replace("'", '"')) if v.startswith("'") else json.loads(v)
            else:
                try:
                    data[k] = float(v) if "." in v else int(v)
                except ValueError:
                    data[k] = v
        if "path" not in data:
            data["path"] = str(project_dir.resolve())
        if "workspace_path" not in data:
            data["workspace_path"] = str(project_dir / "workspace")
        if "id" not in data:
            data["id"] = project_dir.name
        return cls.from_dict(data)

    def save_json(self) -> None:
        """Canonical machine-readable project file."""
        self.updated_at = time.time()
        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "project.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.save()


@dataclass
class SessionMeta:
    id: str
    project_id: str
    title: str
    type: SessionType
    status: SessionStatus = SessionStatus.ACTIVE
    parent_session_id: str | None = None
    branch_name: str | None = None
    container_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "type": self.type.value,
            "status": self.status.value,
            "parent_session_id": self.parent_session_id,
            "branch_name": self.branch_name,
            "container_id": self.container_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMeta:
        return cls(
            id=str(data["id"]),
            project_id=str(data["project_id"]),
            title=str(data.get("title") or data["id"]),
            type=SessionType(str(data.get("type") or "main")),
            status=SessionStatus(str(data.get("status") or "active")),
            parent_session_id=data.get("parent_session_id"),
            branch_name=data.get("branch_name"),
            container_id=data.get("container_id"),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


def session_jsonl_path(project: Project, session_id: str) -> Path:
    return project.sessions_dir / f"{session_id}.jsonl"


def session_meta_path(project: Project, session_id: str) -> Path:
    return project.sessions_dir / f"{session_id}.meta.json"


def save_session_meta(project: Project, meta: SessionMeta) -> None:
    meta.updated_at = time.time()
    project.sessions_dir.mkdir(parents=True, exist_ok=True)
    session_meta_path(project, meta.id).write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_session_meta(project: Project, session_id: str) -> SessionMeta:
    path = session_meta_path(project, session_id)
    if not path.is_file():
        raise AriadneError(
            app_error("ARIADNE_CONFIG_INVALID", f"session not found: {session_id}")
        )
    return SessionMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))


def append_transcript(project: Project, session_id: str, row: dict[str, Any]) -> None:
    path = session_jsonl_path(project, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_transcript(project: Project, session_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
    path = session_jsonl_path(project, session_id)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
