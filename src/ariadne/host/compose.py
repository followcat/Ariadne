from __future__ import annotations

from pathlib import Path

from ..agent import Agent
from ..config import Settings
from ..errors import AriadneError, app_error
from ..kernel.turn import TurnApplication
from ..memory.curated import CuratedStore
from ..memory.facade import MemoryFacade
from ..memory.semantic import SemanticIndex
from ..memory.state import ConversationStateStore
from ..memory.summary import TurnSummaryStore
from ..memory.transcript import TranscriptStore
from ..model.openai_chat import OpenAIChatModel
from ..sandbox.local import LocalWorkdirSandbox
from ..sandbox.null import NullSandbox
from ..skills.store import SkillStore
from ..tools.registry import build_default_registry


def compose_agent(settings: Settings) -> Agent:
    if not settings.base_url or not settings.api_key:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                "Missing BASE_URL or API_KEY. Create .env (see .env.example) or export them.",
            )
        )

    data_dir = settings.resolved_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    if settings.sandbox in {"local", "workdir", "localworkdir"}:
        backend = LocalWorkdirSandbox(workspace=settings.workspace, data_dir=data_dir)
    elif settings.sandbox in {"null", "none", "off"}:
        backend = NullSandbox()
    else:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"Unknown sandbox backend: {settings.sandbox!r} (use local|null)",
            )
        )

    model = OpenAIChatModel(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
    transcript = TranscriptStore(path=data_dir / "sessions" / f"{settings.session_id}.jsonl")
    memory = MemoryFacade(
        transcript=transcript,
        curated=CuratedStore(path=data_dir / "memory" / "curated.json"),
        state=ConversationStateStore(path=data_dir / "memory" / "state.json"),
        summaries=TurnSummaryStore(path=data_dir / "memory" / "summaries.json"),
        semantic=SemanticIndex(path=data_dir / "memory" / "semantic.json"),
    )

    skill_dirs: list[Path] = []
    # src/ariadne/host/compose.py -> parents[3] == repo root for editable src layout
    repo_root = Path(__file__).resolve().parents[3]
    for candidate in (
        repo_root / "skills" / "builtin",
        settings.workspace / "skills",
        settings.skills_dir,
    ):
        if candidate is not None and Path(candidate).is_dir():
            skill_dirs.append(Path(candidate))
    skills = SkillStore.from_dirs(skill_dirs, strict=False) if skill_dirs else SkillStore({})

    tools = build_default_registry(memory=memory, skills=skills)
    turn_app = TurnApplication(
        model=model,
        tools=tools,
        sandbox_backend=backend,
        memory=memory,
        skills=skills,
        tool_loop_limit=settings.tool_loop_limit,
        prefer_deferred_tools=settings.prefer_deferred_tools,
    )
    return Agent(turn_app=turn_app, session_id=settings.session_id, model=settings.model)
