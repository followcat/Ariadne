"""FastAPI web host: auth, BYOK provider binding, turns + SSE, sessions."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..cli.render import render_json
from ..config import Settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from .users import UserStore

STATIC_DIR = Path(__file__).parent / "static"


def _persist_turn_tools(
    *,
    sessions_dir: Path,
    session_id: str,
    result_payload: dict[str, Any] | None,
    status: str | None = None,
) -> dict[str, Any] | None:
    """Write turn tool history to disk (survives restart)."""
    if not session_id or not result_payload:
        return None
    from ..cli.turn_history import append_turn_from_result_payload

    return append_turn_from_result_payload(
        sessions_dir,
        session_id,
        result_payload,
        status=status,
    )


class RegisterBody(BaseModel):
    username: str
    password: str


class ProviderBody(BaseModel):
    base_url: str
    api_key: str
    model: str


class TurnBody(BaseModel):
    input: str
    task_mode: bool = False
    session_id: str | None = None
    atelier_id: str | None = None
    atelier_session: str | None = None  # main | branch-<name> | bare branch name


class ImagePart(BaseModel):
    mime: str
    data_base64: str
    name: str | None = None


class TurnStreamBody(BaseModel):
    input: str = ""
    task_mode: bool = False
    session_id: str | None = None
    images: list[ImagePart] = []
    atelier_id: str | None = None
    atelier_session: str | None = None


class AtelierCreateBody(BaseModel):
    """name = display title (中文可用); optional id = filesystem slug."""

    name: str
    id: str | None = None
    from_path: str | None = None
    no_scan: bool = False


class AtelierBranchBody(BaseModel):
    name: str
    initial_message: str | None = None


class KnowledgePutBody(BaseModel):
    content: str


class KnowledgeApplyItem(BaseModel):
    section: str = "关键决策"
    type: str = "add"  # add | modify | remove
    old_text: str = ""
    new_text: str = ""
    evidence: str = ""


class KnowledgeApplyBody(BaseModel):
    updates: list[KnowledgeApplyItem]


class SessionPatchBody(BaseModel):
    title: str | None = None
    refresh_title: bool = False
    force: bool = False  # force auto-refresh even if user-set title


class PluginBody(BaseModel):
    config: dict[str, str]


class UserModelBody(BaseModel):
    type: str
    key: str
    value: Any
    confidence: float = 1.0
    scope: str = "user"
    workspace_key: str = ""
    session_id: str = ""
    expected_revision: int | None = None


class UserModelExpireBody(BaseModel):
    expected_revision: int


class SkillPatchProposalBody(BaseModel):
    description: str
    body: str
    keywords: list[str] = []
    evidence: list[str]
    expected_version: str


class SkillPatchRejectBody(BaseModel):
    reason: str


class SkillCorrectionBody(BaseModel):
    turn_id: str
    skill_name: str
    reason: str


class SkillRankingBody(BaseModel):
    enabled: bool


class ScheduledGoalBody(BaseModel):
    session_id: str
    goal: str
    check: dict[str, Any]
    interval_seconds: float = 3600.0
    next_run_at: float | None = None


class ScheduledGoalStatusBody(BaseModel):
    status: str
    expected_revision: int


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Ariadne Web")
    users = UserStore(settings.resolved_data_dir / "web" / "users.json")

    def current_user(authorization: str = Header(default="")) -> str:
        token = authorization.removeprefix("Bearer ").strip()
        username = users.username_for_token(token) if token else None
        if username is None:
            raise HTTPException(status_code=401, detail="invalid or missing token")
        return username

    def _project_root() -> Path:
        """Serve-process project folder (CLI cwd / --workspace). Always the project root."""
        root = settings.workspace.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _user_data_dir(username: str) -> Path:
        return settings.resolved_data_dir / "web" / "users" / username

    def _user_model_store(username: str) -> Any:
        from ..memory.user_model import UserModelStore

        return UserModelStore(_user_data_dir(username) / "memory" / "user_model.json")

    def _user_skill_store(username: str) -> Any:
        from ..skills.store import SkillStore

        root = _user_data_dir(username) / "skills" / "user"
        root.mkdir(parents=True, exist_ok=True)
        return SkillStore.from_dir(
            root,
            strict=False,
            user_root=root,
            namespace="user",
        )

    def _skill_outcome_ledger(username: str) -> Any:
        from ..skills.outcomes import SkillOutcomeLedger

        return SkillOutcomeLedger(_user_data_dir(username) / "skills" / "outcomes.json")

    def _scheduled_goals(username: str) -> Any:
        from ..tasks.scheduler import ScheduledGoalStore

        return ScheduledGoalStore(_user_data_dir(username) / "scheduled_goals.sqlite3")

    def _workspace_root_for(
        username: str,
        *,
        atelier_id: str | None = None,
        atelier_session: str | None = None,
    ) -> Path:
        """Active /workspace binding (design/web-workspace.md).

        Single rule — no serve-time mode switch:
        - ordinary chat → serve open folder (settings.workspace)
        - atelier main/branch → that session's tree under the account ateliers root
        Chat sessions share the same binding; only atelier branches isolate a full tree.
        """
        if atelier_id:
            mgr = _atelier_mgr(username)
            try:
                project = mgr.get_project(atelier_id)
                session = _resolve_atelier_session(mgr, atelier_id, atelier_session)
            except AriadneError as exc:
                raise HTTPException(status_code=404, detail=exc.error.message) from exc
            root = project.session_workspace(session).resolve()
            root.mkdir(parents=True, exist_ok=True)
            return root
        return _project_root()

    def _settings_for(username: str) -> Settings:
        provider = users.get_provider(username)
        if not provider:
            raise HTTPException(status_code=400, detail="provider not configured (PUT /api/me/provider)")
        user_data = _user_data_dir(username)
        user_data.mkdir(parents=True, exist_ok=True)
        # Agent sandbox /workspace must match browse APIs for this user.
        # plugins_dir stays on the account even when atelier rebinds data_dir.
        # Per-account isolation (design/memory-scopes.md): never fall back to
        # shared ~/.ariadne/memory for Web — bind user_id + user memory root.
        user_memory = user_data / "memory"
        user_memory.mkdir(parents=True, exist_ok=True)
        return dataclasses.replace(
            settings,
            base_url=provider["base_url"],
            api_key=provider["api_key"],
            model=provider["model"],
            workspace=_workspace_root_for(username),
            data_dir=user_data,
            plugins_dir=user_data,
            merge_home_plugins=False,  # web users only get their own plugins
            user_id=username,
            user_memory_dir=user_memory,
        )

    def _atelier_mgr(username: str) -> Any:
        from ..atelier.manager import AtelierManager

        root = _user_data_dir(username) / "ateliers"
        root.mkdir(parents=True, exist_ok=True)
        return AtelierManager(root=root)

    def _resolve_atelier_session(mgr: Any, project_id: str, session_ref: str | None) -> Any:
        sid = (session_ref or "main").strip() or "main"
        if sid == "main":
            return mgr.get_or_create_main_session(project_id)
        try:
            return mgr.get_session(project_id, sid)
        except AriadneError:
            return mgr.get_session(project_id, f"branch-{sid}")

    def _settings_for_atelier(
        username: str, *, atelier_id: str, atelier_session: str | None
    ) -> tuple[Settings, Any, Any]:
        """Bind workspace + knowledge prompt + atelier data_dir for a turn."""
        from ..atelier.runner import settings_for_atelier

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
            session = _resolve_atelier_session(mgr, atelier_id, atelier_session)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        base = _settings_for(username)
        # Prefer local sandbox in web atelier when docker unavailable is handled
        # by compose; keep user sandbox preference from serve settings.
        bound = settings_for_atelier(project, session, base)
        return bound, project, session

    @app.post("/api/auth/register")
    def register(body: RegisterBody) -> dict[str, str]:
        try:
            token = users.register(body.username, body.password)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"token": token, "username": body.username}

    @app.post("/api/auth/login")
    def login(body: RegisterBody) -> dict[str, str]:
        try:
            token = users.login(body.username, body.password)
        except AriadneError as exc:
            raise HTTPException(status_code=401, detail=exc.error.message) from exc
        return {"token": token, "username": body.username}

    @app.get("/api/me")
    def me(username: str = Depends(current_user)) -> dict[str, Any]:
        provider = users.get_provider(username)
        ws = _workspace_root_for(username)
        return {
            "username": username,
            "provider_configured": bool(provider),
            "base_url": provider.get("base_url", ""),
            "model": provider.get("model", ""),
            # Active /workspace host path — models often print real FS paths; UI maps them.
            "workspace": str(ws),
            # Derived: "project" = serve host dir (internal key, not UI 项目); or atelier.
            "workspace_binding": "project",
            "project_root": str(_project_root()),
        }

    def _resolve_workspace_path(
        raw_path: str,
        *,
        username: str,
        must_exist: bool = True,
        atelier_id: str | None = None,
        atelier_session: str | None = None,
    ) -> Path:
        """Map /workspace/... , relative, or host-absolute-under-root into active workspace."""
        root = _workspace_root_for(
            username, atelier_id=atelier_id, atelier_session=atelier_session
        )
        raw = (raw_path or "").strip() or "/workspace"
        if raw in {"/workspace", "workspace", ".", ""}:
            target = root
            rel = ""
        elif raw.startswith("/workspace/"):
            rel = raw[len("/workspace/") :]
            target = (root / rel).resolve()
        elif raw.startswith("workspace/"):
            rel = raw[len("workspace/") :]
            target = (root / rel).resolve()
        elif raw.startswith("/"):
            # Host absolute path (e.g. /home/…/project/plot.png). Models often print
            # real FS paths; allow only when resolved path stays under workspace root.
            try:
                candidate = Path(raw).expanduser().resolve()
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"invalid path: {exc}") from exc
            if candidate != root and root not in candidate.parents:
                raise HTTPException(
                    status_code=400,
                    detail="path must be under /workspace (sandbox root)",
                )
            target = candidate
            rel = "" if candidate == root else candidate.relative_to(root).as_posix()
        else:
            rel = raw
            target = (root / rel).resolve()
        if rel and ".." in Path(rel).parts:
            raise HTTPException(status_code=400, detail="path escapes workspace")
        if root not in target.parents and target != root:
            raise HTTPException(status_code=400, detail="path escapes workspace")
        if must_exist and not target.exists():
            raise HTTPException(status_code=404, detail=f"path not found: {raw}")
        return target

    def _virtual_path(
        target: Path,
        *,
        username: str,
        atelier_id: str | None = None,
        atelier_session: str | None = None,
    ) -> str:
        root = _workspace_root_for(
            username, atelier_id=atelier_id, atelier_session=atelier_session
        )
        if target == root:
            return "/workspace"
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="path escapes workspace") from exc
        return f"/workspace/{rel}"

    @app.get("/api/workspace/list")
    def workspace_list(
        path: str = Query(default="/workspace", description="Directory under /workspace"),
        atelier_id: str | None = Query(default=None),
        atelier_session: str | None = Query(default=None),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        """List a directory in the sandbox workspace (Codex-style file browser)."""
        target = _resolve_workspace_path(
            path,
            username=username,
            atelier_id=atelier_id,
            atelier_session=atelier_session,
        )
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="path is not a directory")
        # Hide common noise / secrets at listing time
        skip_names = {
            ".git",
            ".venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".env",
            ".DS_Store",
        }
        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                target.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"cannot list: {exc}") from exc
        for child in children:
            name = child.name
            if name in skip_names or name.startswith(".env"):
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            kind = "dir" if child.is_dir() else "file"
            entries.append(
                {
                    "name": name,
                    "path": _virtual_path(
                        child,
                        username=username,
                        atelier_id=atelier_id,
                        atelier_session=atelier_session,
                    ),
                    "kind": kind,
                    "size": int(st.st_size) if kind == "file" else 0,
                    "mtime": float(st.st_mtime),
                }
            )
        parent = target.parent
        root = _workspace_root_for(
            username, atelier_id=atelier_id, atelier_session=atelier_session
        )
        parent_path = None
        if target != root and (root in parent.parents or parent == root):
            parent_path = _virtual_path(
                parent,
                username=username,
                atelier_id=atelier_id,
                atelier_session=atelier_session,
            )
        return {
            "path": _virtual_path(
                target,
                username=username,
                atelier_id=atelier_id,
                atelier_session=atelier_session,
            ),
            "parent": parent_path,
            "entries": entries,
            "workspace": str(root),
            "workspace_binding": "atelier" if atelier_id else "project",
            "project_root": str(_project_root()),
            "atelier_id": atelier_id,
            "atelier_session": atelier_session or "main",
        }

    @app.get("/api/workspace/read")
    def workspace_read(
        path: str = Query(..., description="File path under /workspace"),
        max_bytes: int = Query(default=512_000, ge=1024, le=2_000_000),
        atelier_id: str | None = Query(default=None),
        atelier_session: str | None = Query(default=None),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        """Read a text file from workspace (preview). Binary files are flagged."""
        target = _resolve_workspace_path(
            path,
            username=username,
            atelier_id=atelier_id,
            atelier_session=atelier_session,
        )
        if not target.is_file():
            raise HTTPException(status_code=400, detail="path is not a file")
        size = target.stat().st_size
        raw = target.read_bytes()[: max_bytes + 1]
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        # Heuristic: treat as binary if NUL or high ratio of non-text
        binary = b"\x00" in raw[:4096]
        text = ""
        encoding = "utf-8"
        if not binary:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("gb18030")
                    encoding = "gb18030"
                except UnicodeDecodeError:
                    binary = True
                    text = ""
        vpath = _virtual_path(
            target,
            username=username,
            atelier_id=atelier_id,
            atelier_session=atelier_session,
        )
        dl = f"/api/workspace/file?path={vpath}"
        if atelier_id:
            dl += f"&atelier_id={atelier_id}"
            if atelier_session:
                dl += f"&atelier_session={atelier_session}"
        return {
            "path": vpath,
            "name": target.name,
            "size": size,
            "truncated": truncated,
            "binary": binary,
            "encoding": encoding if not binary else None,
            "text": text if not binary else None,
            "download_path": dl,
        }

    @app.get("/api/workspace/file")
    def workspace_file(
        path: str = Query(..., description="Sandbox path e.g. /workspace/plot.png"),
        atelier_id: str | None = Query(default=None),
        atelier_session: str | None = Query(default=None),
        username: str = Depends(current_user),
    ) -> FileResponse:
        """Serve a file from the account's active /workspace root.

        Used by the web UI to inline 走势图 / plots written by sandbox tools.
        Auth required; path confined to active open-folder or atelier root (web-workspace.md).
        """
        target = _resolve_workspace_path(
            path,
            username=username,
            atelier_id=atelier_id,
            atelier_session=atelier_session,
        )
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"file not found: {path}")
        media = "application/octet-stream"
        suffix = target.suffix.lower()
        if suffix in {".png"}:
            media = "image/png"
        elif suffix in {".jpg", ".jpeg"}:
            media = "image/jpeg"
        elif suffix in {".gif"}:
            media = "image/gif"
        elif suffix in {".webp"}:
            media = "image/webp"
        elif suffix in {".svg"}:
            media = "image/svg+xml"
        elif suffix in {".txt", ".md", ".py", ".json", ".csv", ".log", ".yml", ".yaml"}:
            media = "text/plain; charset=utf-8"
        return FileResponse(target, media_type=media, filename=target.name)

    @app.put("/api/me/provider")
    def put_provider(body: ProviderBody, username: str = Depends(current_user)) -> dict[str, str]:
        try:
            users.set_provider(
                username, base_url=body.base_url, api_key=body.api_key, model=body.model
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "ok"}

    @app.get("/api/me/user-model")
    def get_user_model(
        session_id: str = Query(default=""),
        include_expired: bool = Query(default=False),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            entries = _user_model_store(username).list(
                workspace_key=str(_workspace_root_for(username).resolve()),
                session_id=session_id,
                include_expired=include_expired,
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"entries": entries}

    @app.post("/api/me/user-model")
    def create_user_model_entry(
        body: UserModelBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        workspace_key = body.workspace_key
        if body.scope == "workspace" and not workspace_key:
            workspace_key = str(_workspace_root_for(username).resolve())
        try:
            return _user_model_store(username).upsert(
                entry_type=body.type,
                key=body.key,
                value=body.value,
                source="user_explicit",
                confidence=body.confidence,
                scope=body.scope,
                workspace_key=workspace_key,
                session_id=body.session_id,
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc

    @app.put("/api/me/user-model/{entry_id}")
    def update_user_model_entry(
        entry_id: str,
        body: UserModelBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        if body.expected_revision is None:
            raise HTTPException(status_code=400, detail="expected_revision is required")
        workspace_key = body.workspace_key
        if body.scope == "workspace" and not workspace_key:
            workspace_key = str(_workspace_root_for(username).resolve())
        try:
            return _user_model_store(username).upsert(
                entry_id=entry_id,
                expected_revision=body.expected_revision,
                entry_type=body.type,
                key=body.key,
                value=body.value,
                source="user_explicit",
                confidence=body.confidence,
                scope=body.scope,
                workspace_key=workspace_key,
                session_id=body.session_id,
            )
        except AriadneError as exc:
            status = 409 if exc.error.code == "ARIADNE_USER_MODEL_CONFLICT" else 400
            raise HTTPException(status_code=status, detail=exc.error.message) from exc

    @app.delete("/api/me/user-model/{entry_id}")
    def expire_user_model_entry(
        entry_id: str,
        body: UserModelExpireBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _user_model_store(username).expire(
                entry_id=entry_id,
                expected_revision=body.expected_revision,
                source="user_explicit",
            )
        except AriadneError as exc:
            status = 409 if exc.error.code == "ARIADNE_USER_MODEL_CONFLICT" else 400
            raise HTTPException(status_code=status, detail=exc.error.message) from exc

    @app.get("/api/me/skill-patches")
    def list_skill_patches(
        status: str | None = Query(default=None),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return {"proposals": _user_skill_store(username).patches().list(status=status)}
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc

    @app.post("/api/me/skills/{name}/patch-proposals")
    def propose_skill_patch(
        name: str,
        body: SkillPatchProposalBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _user_skill_store(username).patches().propose(
                name=name,
                description=body.description,
                body=body.body,
                keywords=body.keywords,
                evidence=body.evidence,
                expected_version=body.expected_version,
            )
        except AriadneError as exc:
            status = 409 if exc.error.code == "ARIADNE_SKILL_PATCH_CONFLICT" else 400
            raise HTTPException(status_code=status, detail=exc.error.message) from exc

    @app.post("/api/me/skill-patches/{proposal_id}/confirm")
    def confirm_skill_patch(
        proposal_id: str,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _user_skill_store(username).patches().confirm(
                proposal_id=proposal_id,
                confirmed_by=username,
            )
        except AriadneError as exc:
            status = 409 if exc.error.code == "ARIADNE_SKILL_PATCH_CONFLICT" else 400
            raise HTTPException(status_code=status, detail=exc.error.message) from exc

    @app.post("/api/me/skill-patches/{proposal_id}/reject")
    def reject_skill_patch(
        proposal_id: str,
        body: SkillPatchRejectBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _user_skill_store(username).patches().reject(
                proposal_id=proposal_id,
                rejected_by=username,
                reason=body.reason,
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc

    @app.get("/api/me/skill-outcomes")
    def get_skill_outcomes(
        skill_name: str | None = Query(default=None),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        ledger = _skill_outcome_ledger(username)
        return {
            "ranking_enabled": ledger.ranking_enabled(),
            "events": ledger.list_events(skill_name=skill_name),
            "adjustment": (
                dataclasses.asdict(ledger.adjustment(skill_name)) if skill_name else None
            ),
        }

    @app.post("/api/me/skill-outcomes/corrections")
    def correct_skill_outcome(
        body: SkillCorrectionBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            event_id = _skill_outcome_ledger(username).record_user_correction(
                turn_id=body.turn_id,
                skill_name=body.skill_name,
                reason=body.reason,
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"event_id": event_id, "status": "recorded"}

    @app.put("/api/me/skill-outcomes/ranking")
    def put_skill_outcome_ranking(
        body: SkillRankingBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        return _skill_outcome_ledger(username).set_ranking_enabled(body.enabled)

    @app.get("/api/me/scheduled-goals")
    def list_scheduled_goals(
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        return {"scheduled_goals": _scheduled_goals(username).list(user_id=username)}

    @app.post("/api/me/scheduled-goals")
    def create_scheduled_goal(
        body: ScheduledGoalBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _scheduled_goals(username).create(
                user_id=username,
                session_id=body.session_id,
                goal=body.goal,
                check=body.check,
                interval_seconds=body.interval_seconds,
                next_run_at=body.next_run_at,
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc

    @app.post("/api/me/scheduled-goals/run-due")
    def run_due_scheduled_goals(
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        from ..tasks.verify import DeterministicVerifier

        try:
            results = _scheduled_goals(username).run_due(
                user_id=username,
                worker_id=f"web-cron:{username}:{uuid.uuid4().hex[:12]}",
                verifier=DeterministicVerifier(_workspace_root_for(username)),
            )
        except AriadneError as exc:
            raise HTTPException(status_code=409, detail=exc.error.message) from exc
        return {"results": results, "count": len(results)}

    @app.put("/api/me/scheduled-goals/{schedule_id}")
    def update_scheduled_goal_status(
        schedule_id: str,
        body: ScheduledGoalStatusBody,
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return _scheduled_goals(username).set_status(
                schedule_id=schedule_id,
                user_id=username,
                status=body.status,
                expected_revision=body.expected_revision,
            )
        except AriadneError as exc:
            status_code = 409 if exc.error.code == "ARIADNE_SCHEDULE_CONFLICT" else 400
            raise HTTPException(status_code=status_code, detail=exc.error.message) from exc

    @app.get("/api/me/goal-notifications")
    def get_goal_notifications(
        unread_only: bool = Query(default=False),
        username: str = Depends(current_user),
    ) -> dict[str, Any]:
        return {
            "notifications": _scheduled_goals(username).notifications(
                user_id=username,
                unread_only=unread_only,
            )
        }

    def _parse_images(parts: list[ImagePart] | None) -> list[Any]:
        from ..multimodal import image_from_base64

        if not parts:
            return []
        out = []
        for part in parts:
            out.append(
                image_from_base64(
                    part.mime,
                    part.data_base64,
                    name=part.name or "image.png",
                )
            )
        return out

    @app.post("/api/turns")
    async def run_turn(body: TurnBody, username: str = Depends(current_user)) -> Any:
        project = None
        session = None
        if body.atelier_id:
            user_settings, project, session = _settings_for_atelier(
                username,
                atelier_id=body.atelier_id,
                atelier_session=body.atelier_session,
            )
        else:
            user_settings = _settings_for(username)
            if body.session_id:
                user_settings = dataclasses.replace(user_settings, session_id=body.session_id)
        agent = compose_agent(user_settings)
        try:
            if body.task_mode:
                result = await agent.run(body.input, metadata={"task_mode": True})
            else:
                result = await agent.run(body.input)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        out = json.loads(render_json(result))
        # Persist tools for both atelier and plain sessions
        turn_sid = str(out.get("session_id") or getattr(result, "session_id", "") or "")
        if project is not None and session is not None:
            _persist_turn_tools(
                sessions_dir=project.sessions_dir,
                session_id=session.id,
                result_payload=out,
            )
        elif turn_sid:
            _persist_turn_tools(
                sessions_dir=_user_data_dir(username) / "sessions",
                session_id=body.session_id or turn_sid,
                result_payload=out,
            )
        if project is not None and session is not None:
            from ..atelier.models import append_transcript
            from ..atelier.runner import update_knowledge_after_turn

            # Dual-write transcript; main may small-step update 便签 when 约定 present.
            asst = str(getattr(result, "text", None) or out.get("text") or "")
            tid = str(out.get("turn_id") or getattr(result, "turn_id", "") or "")
            append_transcript(
                project,
                session.id,
                {
                    "role": "user",
                    "content": body.input,
                    "session_id": session.id,
                    **({"turn_id": tid} if tid else {}),
                },
            )
            append_transcript(
                project,
                session.id,
                {
                    "role": "assistant",
                    "content": asst,
                    "session_id": session.id,
                    **({"turn_id": tid} if tid else {}),
                },
            )
            try:
                krep = await update_knowledge_after_turn(
                    project,
                    session,
                    user_text=body.input,
                    assistant_text=asst,
                    settings=user_settings,
                )
                out["atelier_knowledge"] = krep
            except Exception:
                out["atelier_knowledge"] = {
                    "updated": False,
                    "reason": "error",
                    "source": "none",
                }
        return out

    async def _sse_for_turn(
        *,
        username: str,
        text: str,
        session_id: str | None,
        images: list[Any] | None,
        task_mode: bool = False,
        atelier_id: str | None = None,
        atelier_session: str | None = None,
    ) -> StreamingResponse:
        project = None
        session = None
        if atelier_id:
            user_settings, project, session = _settings_for_atelier(
                username, atelier_id=atelier_id, atelier_session=atelier_session
            )
            user_settings = dataclasses.replace(user_settings, stream=True)
        else:
            user_settings = _settings_for(username)
            # SSE path always wants model token streaming (answer + thinking deltas).
            if session_id:
                user_settings = dataclasses.replace(
                    user_settings, session_id=session_id, stream=True
                )
            else:
                user_settings = dataclasses.replace(user_settings, stream=True)
        agent = compose_agent(user_settings)

        async def events():
            try:
                stream_events = (
                    agent.run_stream(
                        text,
                        images=images or None,
                        metadata={"task_mode": True},
                    )
                    if task_mode
                    else agent.run_stream(text, images=images or None)
                )
                async for event in stream_events:
                    if event.kind in {"turn_completed", "turn_failed"}:
                        result = event.data.get("result")
                        result_payload = (
                            json.loads(render_json(result)) if result else None
                        )
                        payload: dict[str, Any] = {
                            "kind": event.kind,
                            "result": result_payload,
                        }
                        # Persist tools for restart-safe turn badges (atelier + plain)
                        persist_sid = ""
                        persist_dir: Path | None = None
                        if project is not None and session is not None:
                            persist_sid = session.id
                            persist_dir = project.sessions_dir
                        else:
                            persist_sid = str(
                                session_id
                                or (result_payload or {}).get("session_id")
                                or user_settings.session_id
                                or ""
                            )
                            persist_dir = _user_data_dir(username) / "sessions"
                        if persist_sid and persist_dir is not None and result_payload:
                            turn_row = _persist_turn_tools(
                                sessions_dir=persist_dir,
                                session_id=persist_sid,
                                result_payload=result_payload,
                                status=str(
                                    (result_payload or {}).get("status")
                                    or (
                                        "failed"
                                        if event.kind == "turn_failed"
                                        else "completed"
                                    )
                                ),
                            )
                            if turn_row:
                                payload["turn_record"] = {
                                    "turn_id": turn_row.get("turn_id"),
                                    "turn_index": turn_row.get("turn_index"),
                                    "tool_count": turn_row.get("tool_count", 0),
                                }
                        if (
                            event.kind == "turn_completed"
                            and project is not None
                            and session is not None
                            and result is not None
                        ):
                            from ..atelier.models import append_transcript
                            from ..atelier.runner import update_knowledge_after_turn

                            asst = str(getattr(result, "text", None) or "")
                            if not asst and result_payload:
                                asst = str(result_payload.get("text") or "")
                            tid = str(
                                (result_payload or {}).get("turn_id")
                                or getattr(result, "turn_id", "")
                                or ""
                            )
                            append_transcript(
                                project,
                                session.id,
                                {
                                    "role": "user",
                                    "content": text,
                                    "session_id": session.id,
                                    **({"turn_id": tid} if tid else {}),
                                },
                            )
                            append_transcript(
                                project,
                                session.id,
                                {
                                    "role": "assistant",
                                    "content": asst,
                                    "session_id": session.id,
                                    **({"turn_id": tid} if tid else {}),
                                },
                            )
                            try:
                                krep = await update_knowledge_after_turn(
                                    project,
                                    session,
                                    user_text=text,
                                    assistant_text=asst,
                                    settings=user_settings,
                                )
                                payload["atelier_knowledge"] = krep
                            except Exception:
                                payload["atelier_knowledge"] = {
                                    "updated": False,
                                    "reason": "error",
                                    "source": "none",
                                }
                    else:
                        payload = {"kind": event.kind, "data": event.data}
                    yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
                    await asyncio.sleep(0)
            except AriadneError as exc:
                # Surface multimodal / config failures as a terminal SSE event
                payload = {
                    "kind": "turn_failed",
                    "error": {
                        "code": exc.error.code,
                        "message": exc.error.message,
                        "details": exc.error.details,
                    },
                    "result": None,
                }
                yield f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/turns/stream")
    async def stream_turn(
        input: str,
        session_id: str | None = None,
        task_mode: bool = False,
        atelier_id: str | None = None,
        atelier_session: str | None = None,
        username: str = Depends(current_user),
    ) -> StreamingResponse:
        # Text-only GET (kept for simple clients / e2e)
        return await _sse_for_turn(
            username=username,
            text=input,
            session_id=session_id,
            images=None,
            task_mode=task_mode,
            atelier_id=atelier_id,
            atelier_session=atelier_session,
        )

    @app.post("/api/turns/stream")
    async def stream_turn_post(
        body: TurnStreamBody, username: str = Depends(current_user)
    ) -> StreamingResponse:
        try:
            images = _parse_images(body.images)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return await _sse_for_turn(
            username=username,
            text=body.input or "",
            session_id=body.session_id,
            images=images,
            task_mode=body.task_mode,
            atelier_id=body.atelier_id,
            atelier_session=body.atelier_session,
        )

    def _user_data(username: str) -> Path:
        return settings.resolved_data_dir / "web" / "users" / username

    @app.get("/api/sessions")
    def sessions(username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import list_sessions

        user_data = _user_data(username)
        return [
            {
                "session_id": s.session_id,
                "turns": s.turns,
                "mtime": s.mtime,
                "preview": s.preview,
                "title": s.title,
                "title_source": s.title_source,
            }
            for s in list_sessions(user_data)
        ]

    @app.post("/api/sessions")
    def create_session(username: str = Depends(current_user)) -> Any:
        """Allocate a new empty session id (transcript created on first turn)."""
        import secrets

        sid = f"web-{secrets.token_hex(4)}"
        return {"session_id": sid, "title": "", "title_source": ""}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import get_session_title, load_session_messages, session_exists

        user_data = _user_data(username)
        title, source = get_session_title(user_data, session_id)
        if not session_exists(user_data, session_id):
            # Brand-new id (not yet written) — empty history is fine for UI
            return {
                "session_id": session_id,
                "messages": [],
                "exists": False,
                "title": title,
                "title_source": source,
            }
        from ..cli.sessions import annotate_turn_indices
        from ..cli.turn_history import list_turn_records

        messages = annotate_turn_indices(
            load_session_messages(user_data, session_id, limit=40)
        )
        turns = list_turn_records(user_data / "sessions", session_id, limit=80)
        return {
            "session_id": session_id,
            "messages": messages,
            "turns": turns,
            "exists": True,
            "title": title,
            "title_source": source,
        }

    @app.get("/api/sessions/{session_id}/turns")
    def get_session_turns(
        session_id: str, username: str = Depends(current_user)
    ) -> Any:
        """Tool-call history per turn (survives restart)."""
        from ..cli.turn_history import list_turn_records

        user_data = _user_data(username)
        turns = list_turn_records(user_data / "sessions", session_id, limit=200)
        return {"session_id": session_id, "turns": turns}

    @app.get("/api/sessions/{session_id}/turns/{turn_id}")
    def get_session_turn(
        session_id: str, turn_id: str, username: str = Depends(current_user)
    ) -> Any:
        from ..cli.turn_history import get_turn_record

        user_data = _user_data(username)
        rec = get_turn_record(user_data / "sessions", session_id, turn_id)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"turn not found: {turn_id}")
        return rec

    @app.patch("/api/sessions/{session_id}")
    def patch_session(
        session_id: str, body: SessionPatchBody, username: str = Depends(current_user)
    ) -> Any:
        """Set or auto-refresh the session topic title."""
        from ..cli.sessions import (
            get_session_title,
            refresh_session_title,
            session_exists,
            set_session_title,
        )

        user_data = _user_data(username)
        if body.refresh_title:
            if not session_exists(user_data, session_id):
                raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
            meta = refresh_session_title(user_data, session_id, force=body.force)
            if meta is None:
                raise HTTPException(status_code=400, detail="cannot title an empty session")
            return {
                "session_id": session_id,
                "title": meta.get("title", ""),
                "title_source": meta.get("source", "auto"),
                "skipped": bool(meta.get("skipped")),
            }
        if body.title is not None:
            try:
                meta = set_session_title(user_data, session_id, body.title, source="user")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "session_id": session_id,
                "title": meta["title"],
                "title_source": meta["source"],
            }
        title, source = get_session_title(user_data, session_id)
        return {"session_id": session_id, "title": title, "title_source": source}

    @app.delete("/api/sessions/{session_id}")
    def remove_session(session_id: str, username: str = Depends(current_user)) -> Any:
        from ..cli.sessions import delete_session

        ok = delete_session(_user_data(username), session_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        return {"status": "deleted", "session_id": session_id}

    def _plugin_store_for(username: str) -> Any:
        from ..plugins import PluginStore

        user_data = settings.resolved_data_dir / "web" / "users" / username
        return PluginStore(user_data / "plugins.json")

    # ── Atelier (project workshop) ──────────────────────────────────────────

    @app.get("/api/ateliers")
    def list_ateliers(username: str = Depends(current_user)) -> Any:
        mgr = _atelier_mgr(username)
        return [
            {
                "id": p.id,
                "name": p.name,
                "workspace_path": str(p.workspace_path),
                "path": str(p.path),
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in mgr.list_projects()
        ]

    @app.post("/api/ateliers")
    def create_atelier(body: AtelierCreateBody, username: str = Depends(current_user)) -> Any:
        mgr = _atelier_mgr(username)
        from_path = Path(body.from_path).expanduser() if body.from_path else None
        # Security: only allow from_path under serve project root or user data
        if from_path is not None:
            try:
                resolved = from_path.resolve()
            except OSError as exc:
                raise HTTPException(status_code=400, detail=f"invalid from_path: {exc}") from exc
            allowed_roots = {_project_root(), _user_data_dir(username).resolve()}
            if not any(
                resolved == root or root in resolved.parents for root in allowed_roots
            ):
                # Also allow if path is inside an existing atelier of this user
                ateliers_root = (_user_data_dir(username) / "ateliers").resolve()
                if ateliers_root not in resolved.parents and resolved != ateliers_root:
                    raise HTTPException(
                        status_code=400,
                        detail="from_path must be under project root or your data dir",
                    )
            from_path = resolved
        try:
            project = mgr.create_project(
                body.name,
                project_id=body.id,
                from_path=from_path,
                no_scan=bool(body.no_scan),
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {
            "id": project.id,
            "name": project.name,
            "workspace_path": str(project.workspace_path),
            "path": str(project.path),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        }

    @app.get("/api/ateliers/{atelier_id}")
    def get_atelier(atelier_id: str, username: str = Depends(current_user)) -> Any:
        from ..atelier.knowledge import read_knowledge

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        sessions = [
            {
                "id": s.id,
                "title": s.title,
                "type": s.type.value,
                "status": s.status.value,
                "branch_name": s.branch_name,
                "parent_session_id": s.parent_session_id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in mgr.list_sessions(atelier_id)
        ]
        return {
            "id": project.id,
            "name": project.name,
            "workspace_path": str(project.workspace_path),
            "path": str(project.path),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "config": project.config.to_dict(),
            "sessions": sessions,
            "knowledge_preview": read_knowledge(project)[:400],
        }

    @app.delete("/api/ateliers/{atelier_id}")
    def delete_atelier(
        atelier_id: str,
        yes: bool = Query(default=False),
        username: str = Depends(current_user),
    ) -> Any:
        mgr = _atelier_mgr(username)
        try:
            mgr.delete_project(atelier_id, yes=yes)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "deleted", "id": atelier_id}

    @app.get("/api/ateliers/{atelier_id}/sessions")
    def atelier_sessions(atelier_id: str, username: str = Depends(current_user)) -> Any:
        mgr = _atelier_mgr(username)
        try:
            sessions = mgr.list_sessions(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        return [
            {
                "id": s.id,
                "title": s.title,
                "type": s.type.value,
                "status": s.status.value,
                "branch_name": s.branch_name,
                "parent_session_id": s.parent_session_id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in sessions
        ]

    @app.get("/api/ateliers/{atelier_id}/sessions/{session_id}/messages")
    def atelier_session_messages(
        atelier_id: str, session_id: str, username: str = Depends(current_user)
    ) -> Any:
        from ..atelier.models import read_transcript

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
            session = _resolve_atelier_session(mgr, atelier_id, session_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        from ..cli.sessions import annotate_turn_indices
        from ..cli.turn_history import list_turn_records

        rows = read_transcript(project, session.id, limit=40)
        messages = annotate_turn_indices(
            [
                {
                    "role": r.get("role"),
                    "content": r.get("content") or "",
                    **(
                        {"turn_id": r.get("turn_id")}
                        if r.get("turn_id")
                        else {}
                    ),
                }
                for r in rows
                if r.get("role") in {"user", "assistant"}
            ]
        )
        turns = list_turn_records(project.sessions_dir, session.id, limit=80)
        return {
            "atelier_id": atelier_id,
            "session_id": session.id,
            "type": session.type.value,
            "status": session.status.value,
            "messages": messages,
            "turns": turns,
        }

    @app.get("/api/ateliers/{atelier_id}/sessions/{session_id}/turns")
    def atelier_session_turns(
        atelier_id: str, session_id: str, username: str = Depends(current_user)
    ) -> Any:
        from ..cli.turn_history import list_turn_records

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
            session = _resolve_atelier_session(mgr, atelier_id, session_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        turns = list_turn_records(project.sessions_dir, session.id, limit=200)
        return {
            "atelier_id": atelier_id,
            "session_id": session.id,
            "turns": turns,
        }

    @app.post("/api/ateliers/{atelier_id}/branches")
    def create_branch(
        atelier_id: str, body: AtelierBranchBody, username: str = Depends(current_user)
    ) -> Any:
        mgr = _atelier_mgr(username)
        try:
            meta = mgr.create_branch(
                atelier_id, body.name, initial_message=body.initial_message
            )
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {
            "id": meta.id,
            "title": meta.title,
            "type": meta.type.value,
            "status": meta.status.value,
            "branch_name": meta.branch_name,
        }

    @app.post("/api/ateliers/{atelier_id}/branches/{branch_name}/merge")
    def merge_branch(
        atelier_id: str, branch_name: str, username: str = Depends(current_user)
    ) -> Any:
        mgr = _atelier_mgr(username)
        try:
            # Append short merge note only — no LLM/heuristic decision extract.
            summary = mgr.merge_branch(atelier_id, branch_name)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "merged", "branch": branch_name, "summary": summary}

    @app.post("/api/ateliers/{atelier_id}/branches/{branch_name}/discard")
    def discard_branch(
        atelier_id: str, branch_name: str, username: str = Depends(current_user)
    ) -> Any:
        mgr = _atelier_mgr(username)
        try:
            mgr.discard_branch(atelier_id, branch_name)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "discarded", "branch": branch_name}

    @app.get("/api/ateliers/{atelier_id}/knowledge")
    def get_knowledge(atelier_id: str, username: str = Depends(current_user)) -> Any:
        from ..atelier.knowledge import (
            list_knowledge_history,
            read_knowledge,
            sync_knowledge_from_workspace_if_empty,
        )

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        synced = sync_knowledge_from_workspace_if_empty(project)
        hist = list_knowledge_history(project)
        return {
            "atelier_id": atelier_id,
            "content": read_knowledge(project),
            "path": str(project.knowledge_path),
            "history": [p.name for p in hist[:30]],
            "synced_from_workspace": synced,
        }

    @app.put("/api/ateliers/{atelier_id}/knowledge")
    def put_knowledge(
        atelier_id: str, body: KnowledgePutBody, username: str = Depends(current_user)
    ) -> Any:
        from ..atelier.knowledge import write_knowledge

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        write_knowledge(project, body.content or "", session_id="web-edit")
        return {"status": "ok", "atelier_id": atelier_id}

    @app.post("/api/ateliers/{atelier_id}/knowledge/apply")
    def apply_knowledge(
        atelier_id: str, body: KnowledgeApplyBody, username: str = Depends(current_user)
    ) -> Any:
        from ..atelier.knowledge import (
            KnowledgeUpdate,
            KnowledgeUpdateItem,
            apply_updates,
            read_knowledge,
            write_knowledge,
        )

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        items = []
        for u in body.updates:
            op = (u.type or "add").strip().lower()
            if op not in {"add", "modify", "remove"}:
                raise HTTPException(status_code=400, detail=f"invalid update type: {u.type}")
            items.append(
                KnowledgeUpdateItem(
                    section=u.section or "关键决策",
                    type=op,
                    old_text=u.old_text or "",
                    new_text=u.new_text or "",
                    evidence=u.evidence or "",
                )
            )
        update = KnowledgeUpdate(has_update=bool(items), updates=items)
        current = read_knowledge(project)
        new_content = apply_updates(current, update)
        if new_content != current:
            write_knowledge(project, new_content, session_id="web-apply")
        return {
            "status": "ok",
            "changed": new_content != current,
            "content": new_content,
            "ops": len(items),
        }

    @app.post("/api/ateliers/{atelier_id}/knowledge/refresh")
    def refresh_knowledge(atelier_id: str, username: str = Depends(current_user)) -> Any:
        from ..atelier.knowledge import heuristic_refresh, write_knowledge

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        content = heuristic_refresh(project)
        write_knowledge(project, content, session_id="web-refresh")
        return {"status": "ok", "content": content}

    @app.get("/api/ateliers/{atelier_id}/knowledge/history")
    def knowledge_history(atelier_id: str, username: str = Depends(current_user)) -> Any:
        from ..atelier.knowledge import list_knowledge_history

        mgr = _atelier_mgr(username)
        try:
            project = mgr.get_project(atelier_id)
        except AriadneError as exc:
            raise HTTPException(status_code=404, detail=exc.error.message) from exc
        return {
            "atelier_id": atelier_id,
            "history": [
                {"name": p.name, "mtime": p.stat().st_mtime, "size": p.stat().st_size}
                for p in list_knowledge_history(project)[:50]
            ],
        }

    @app.get("/api/me/plugins")
    def list_my_plugins(username: str = Depends(current_user)) -> Any:
        from ..plugins import PLUGIN_REGISTRY
        from ..plugins.store import display_config

        configured = _plugin_store_for(username).list()
        return [
            {
                "name": name,
                "description": plugin.description,
                "required_config": list(plugin.required_config),
                "enabled": bool(configured.get(name, {}).get("enabled")),
                "configured": name in configured,
                # secrets masked (middle *****); never return raw tokens
                "config": display_config(
                    dict((configured.get(name) or {}).get("config") or {})
                ),
            }
            for name, plugin in sorted(PLUGIN_REGISTRY.items())
        ]

    @app.put("/api/me/plugins/{name}")
    def enable_plugin(name: str, body: PluginBody, username: str = Depends(current_user)) -> Any:
        from ..plugins import PLUGIN_REGISTRY, build_plugin_tools
        from ..plugins.store import looks_masked_value

        plugin = PLUGIN_REGISTRY.get(name)
        if plugin is None:
            raise HTTPException(status_code=404, detail=f"unknown plugin: {name}")
        store = _plugin_store_for(username)
        existing = dict((store.list().get(name) or {}).get("config") or {})
        # Keep stored secrets when the client resubmits a masked placeholder
        # or leaves a secret field blank on re-enable.
        merged: dict[str, str] = {}
        missing: list[str] = []
        for key in plugin.required_config:
            raw = str(body.config.get(key) or "").strip()
            if not raw or looks_masked_value(raw):
                if existing.get(key):
                    merged[key] = str(existing[key])
                else:
                    missing.append(key)
            else:
                merged[key] = raw
        if missing:
            raise HTTPException(status_code=400, detail=f"missing config: {', '.join(missing)}")
        try:
            build_plugin_tools(name, merged)  # validate before persisting
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        store.enable(name, merged)
        return {"status": "enabled", "plugin": name}

    @app.delete("/api/me/plugins/{name}")
    def disable_plugin(name: str, username: str = Depends(current_user)) -> Any:
        try:
            _plugin_store_for(username).disable(name)
        except AriadneError as exc:
            raise HTTPException(status_code=400, detail=exc.error.message) from exc
        return {"status": "disabled", "plugin": name}

    # Vue SPA build (frontend/ → static/dist). Fall back to legacy static/index.html.
    dist_dir = STATIC_DIR / "dist"
    dist_index = dist_dir / "index.html"
    legacy_index = STATIC_DIR / "index.html"

    if dist_dir.is_dir():
        assets = dist_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="vue-assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        if dist_index.is_file():
            return FileResponse(dist_index)
        return FileResponse(legacy_index)

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa_fallback(spa_path: str) -> FileResponse:
        """Serve built SPA files or index for client routes; never shadow /api."""
        if spa_path.startswith("api"):
            raise HTTPException(status_code=404, detail="not found")
        if dist_dir.is_dir():
            candidate = dist_dir / spa_path
            if candidate.is_file():
                return FileResponse(candidate)
            if dist_index.is_file():
                return FileResponse(dist_index)
        raise HTTPException(status_code=404, detail="not found")

    return app
