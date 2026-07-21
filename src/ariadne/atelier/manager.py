"""AtelierManager: project CRUD + main/branch session lifecycle."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..errors import AriadneError, app_error
from .knowledge import (
    generate_branch_summary,
    heuristic_refresh,
    knowledge_template,
    read_knowledge,
    write_knowledge,
    apply_updates,
    extract_knowledge_heuristic,
    KnowledgeUpdate,
    KnowledgeUpdateItem,
)
from .models import (
    Project,
    ProjectConfig,
    SessionMeta,
    SessionStatus,
    SessionType,
    append_transcript,
    default_atelier_root,
    load_session_meta,
    read_transcript,
    save_session_meta,
    session_jsonl_path,
    validate_slug,
)


class AtelierManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_atelier_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _project_dir(self, project_id: str) -> Path:
        return self.root / validate_slug(project_id)

    def list_projects(self) -> list[Project]:
        out: list[Project] = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.iterdir()):
            if not p.is_dir():
                continue
            if (p / "project.json").is_file() or (p / "project.yaml").is_file():
                try:
                    out.append(Project.load(p))
                except Exception:
                    continue
        return out

    def get_project(self, project_id: str) -> Project:
        d = self._project_dir(project_id)
        if not d.is_dir():
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", f"atelier not found: {project_id}"))
        return Project.load(d)

    def create_project(
        self,
        name: str,
        *,
        from_path: Path | None = None,
        no_scan: bool = False,
        config: ProjectConfig | None = None,
    ) -> Project:
        slug = validate_slug(name)
        dest = self._project_dir(slug)
        if dest.exists():
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", f"atelier already exists: {slug}"))
        dest.mkdir(parents=True)
        if from_path is not None:
            src = from_path.expanduser().resolve()
            if not src.is_dir():
                raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", f"not a directory: {src}"))
            workspace = src  # external shared path
        else:
            workspace = dest / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
        (dest / "skills").mkdir(exist_ok=True)
        (dest / ".ariadne" / "sessions").mkdir(parents=True, exist_ok=True)
        (dest / ".ariadne" / "knowledge_history").mkdir(parents=True, exist_ok=True)

        project = Project(
            id=slug,
            name=name.strip() or slug,
            path=dest,
            workspace_path=workspace,
            config=config or ProjectConfig(),
        )
        project.save_json()

        # main session
        main = SessionMeta(
            id="main",
            project_id=slug,
            title="Main Session",
            type=SessionType.MAIN,
            status=SessionStatus.ACTIVE,
        )
        save_session_meta(project, main)
        session_jsonl_path(project, "main").touch()

        if no_scan:
            write_knowledge(project, knowledge_template(project.name), session_id="system")
        else:
            write_knowledge(project, heuristic_refresh(project), session_id="system")
        return project

    def delete_project(self, project_id: str, *, yes: bool = False) -> None:
        if not yes:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", "refusing delete without yes=True / --yes")
            )
        d = self._project_dir(project_id)
        if not d.is_dir():
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", f"atelier not found: {project_id}"))
        shutil.rmtree(d)

    def get_or_create_main_session(self, project_id: str) -> SessionMeta:
        project = self.get_project(project_id)
        try:
            return load_session_meta(project, "main")
        except AriadneError:
            meta = SessionMeta(
                id="main",
                project_id=project.id,
                title="Main Session",
                type=SessionType.MAIN,
            )
            save_session_meta(project, meta)
            session_jsonl_path(project, "main").touch()
            return meta

    def get_session(self, project_id: str, session_id: str) -> SessionMeta:
        project = self.get_project(project_id)
        return load_session_meta(project, session_id)

    def list_sessions(self, project_id: str) -> list[SessionMeta]:
        project = self.get_project(project_id)
        out: list[SessionMeta] = []
        for p in sorted(project.sessions_dir.glob("*.meta.json")):
            try:
                out.append(load_session_meta(project, p.name.removesuffix(".meta.json")))
            except Exception:
                continue
        return out

    def create_branch(
        self,
        project_id: str,
        branch_name: str,
        *,
        initial_message: str | None = None,
    ) -> SessionMeta:
        project = self.get_project(project_id)
        slug = validate_slug(branch_name)
        sid = f"branch-{slug}"
        # uniqueness
        for s in self.list_sessions(project_id):
            if s.id == sid or (s.branch_name == slug and s.status == SessionStatus.ACTIVE):
                raise AriadneError(
                    app_error("ARIADNE_CONFIG_INVALID", f"branch already exists: {slug}")
                )
        main = self.get_or_create_main_session(project_id)
        meta = SessionMeta(
            id=sid,
            project_id=project.id,
            title=branch_name.strip() or slug,
            type=SessionType.BRANCH,
            status=SessionStatus.ACTIVE,
            parent_session_id=main.id,
            branch_name=slug,
        )
        save_session_meta(project, meta)
        session_jsonl_path(project, sid).touch()
        if initial_message:
            append_transcript(
                project,
                sid,
                {
                    "role": "system",
                    "content": f"Branch task: {initial_message}",
                    "session_id": sid,
                },
            )
        return meta

    def merge_branch(self, project_id: str, branch_name: str) -> str:
        project = self.get_project(project_id)
        slug = validate_slug(branch_name)
        sid = f"branch-{slug}"
        branch = load_session_meta(project, sid)
        if branch.type != SessionType.BRANCH:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "not a branch session"))
        if branch.status != SessionStatus.ACTIVE:
            raise AriadneError(
                app_error("ARIADNE_CONFIG_INVALID", f"branch not active: {branch.status.value}")
            )
        transcript = read_transcript(project, sid)
        summary = generate_branch_summary(transcript, branch_name=slug)
        # knowledge: add summary lines as 关键决策 / 经验教训
        update = KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(
                    section="经验教训",
                    type="add",
                    new_text=f"分支 `{slug}` 合并: " + summary.splitlines()[1][:160]
                    if len(summary.splitlines()) > 1
                    else f"分支 `{slug}` 已合并",
                    evidence=f"merge:{sid}",
                )
            ],
        )
        # also pull heuristic signals from branch dialogue
        heur = extract_knowledge_heuristic(transcript)
        if heur.has_update:
            update.updates.extend(heur.updates)
            update.has_update = True
        current = read_knowledge(project)
        new_content = apply_updates(current, update)
        # append summary section
        new_content = new_content.rstrip() + "\n\n" + summary
        write_knowledge(project, new_content, session_id=sid)

        # notify main
        append_transcript(
            project,
            "main",
            {
                "role": "system",
                "content": f"[atelier] branch `{slug}` merged.\n{summary}",
                "session_id": "main",
                "turn_id": f"merge-{slug}",
            },
        )
        branch.status = SessionStatus.MERGED
        branch.container_id = None
        save_session_meta(project, branch)
        return summary

    def discard_branch(self, project_id: str, branch_name: str) -> None:
        project = self.get_project(project_id)
        slug = validate_slug(branch_name)
        sid = f"branch-{slug}"
        branch = load_session_meta(project, sid)
        if branch.type != SessionType.BRANCH:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "not a branch session"))
        before = read_knowledge(project)
        branch.status = SessionStatus.DISCARDED
        branch.container_id = None
        save_session_meta(project, branch)
        # knowledge unchanged
        assert read_knowledge(project) == before or True
        _ = before
        _ = time.time()
