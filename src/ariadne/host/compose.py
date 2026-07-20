from __future__ import annotations

from pathlib import Path

from ..agent import Agent
from ..config import Settings
from ..errors import AriadneError, app_error
from ..kernel.turn import TurnApplication
from ..memory.curated import CuratedStore
from ..memory.embeddings import HashEmbeddingProvider, OpenAIEmbeddingProvider
from ..memory.facade import MemoryFacade
from ..memory.projection import ProjectionWorker
from ..memory.semantic import SemanticIndex
from ..memory.state import ConversationStateStore
from ..memory.summary import TurnSummaryStore
from ..memory.transcript import TranscriptStore
from ..model.openai_chat import OpenAIChatModel
from ..sandbox.active import ActiveSessionManager
from ..sandbox.docker import DockerSandbox
from ..sandbox.local import LocalWorkdirSandbox
from ..sandbox.null import NullSandbox
from ..sandbox.toolbox import get_profile
from ..skills.store import SkillStore
from ..tools.registry import build_default_registry


def compose_agent(settings: Settings) -> Agent:
    if not settings.base_url or not settings.api_key:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                "BASE_URL and API_KEY are required. Put them in .env (see .env.example) or export them.",
            )
        )

    data_dir = settings.resolved_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    profile = get_profile(settings.toolbox_profile)
    docker_image = settings.docker_image or profile.docker_image

    if settings.sandbox in {"local", "workdir", "localworkdir"}:
        backend = LocalWorkdirSandbox(workspace=settings.workspace, data_dir=data_dir)
        backend_name = "local"
    elif settings.sandbox in {"null", "none", "off"}:
        backend = NullSandbox()
        backend_name = "null"
    elif settings.sandbox == "docker":
        backend = DockerSandbox(
            workspace=settings.workspace,
            data_dir=data_dir,
            image=docker_image,
        )
        backend_name = "docker"
    else:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"Unknown sandbox backend: {settings.sandbox!r} (use local|null|docker)",
            )
        )

    active: ActiveSessionManager | None = None
    if settings.sandbox_lifecycle == "active_session":
        active = ActiveSessionManager(
            backend,
            idle_ttl_seconds=settings.idle_ttl_seconds,
            max_ttl_seconds=settings.max_ttl_seconds,
            backend_name=backend_name,
        )

    model = OpenAIChatModel(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )

    if settings.embedding_provider == "openai":
        embedder = OpenAIEmbeddingProvider(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.embedding_model,
        )
    else:
        embedder = HashEmbeddingProvider()

    transcript = TranscriptStore(path=data_dir / "sessions" / f"{settings.session_id}.jsonl")
    state_store = ConversationStateStore(path=data_dir / "memory" / "state.json")
    summary_store = TurnSummaryStore(path=data_dir / "memory" / "summaries.json")
    if settings.summary_mode == "llm":
        from ..memory.llm_summary import make_llm_compressor

        summary_store.compressor = make_llm_compressor(model, max_chars=400, fallback=True)
    memory = MemoryFacade(
        transcript=transcript,
        curated=CuratedStore(path=data_dir / "memory" / "curated.json"),
        state=state_store,
        summaries=summary_store,
        semantic=SemanticIndex(path=data_dir / "memory" / "semantic.json", embedder=embedder),
        projection=ProjectionWorker(
            path=data_dir / "memory" / "projection_jobs.json",
            state_store=state_store,
        ),
        hybrid_semantic=True,
    )

    skill_dirs: list[Path] = []
    skill_namespaces: list[str] = []
    repo_root = Path(__file__).resolve().parents[3]
    user_skills = data_dir / "skills" / "user"
    user_skills.mkdir(parents=True, exist_ok=True)
    for candidate, ns in (
        (repo_root / "skills" / "builtin", "builtin"),
        (settings.workspace / "skills", "workspace"),
        (settings.skills_dir, "local"),
        (user_skills, "user"),
    ):
        if candidate is not None and Path(candidate).is_dir():
            skill_dirs.append(Path(candidate))
            skill_namespaces.append(ns)

    # Strict skill load: invalid packs fail composition (DESIGN_PRINCIPLES fastfail).
    skills = SkillStore.from_dirs(
        skill_dirs,
        strict=True,
        user_root=user_skills,
        embedder=embedder,
        namespaces=skill_namespaces,
        budgets=settings.skill_plan_budgets(),
    )

    tools = build_default_registry(memory=memory, skills=skills)
    from ..plugins import PluginStore, build_plugin_tools

    # plugin configs are user attributes: user-level store first,
    # workspace-level store overrides per plugin name
    plugin_configs: dict[str, dict[str, str]] = {}
    store_paths = []
    if settings.merge_home_plugins:
        store_paths.append(Path.home() / ".ariadne" / "plugins.json")
    store_paths.append(settings.resolved_data_dir / "plugins.json")
    for store_path in store_paths:
        plugin_configs.update(PluginStore(store_path).enabled())
    for plugin_name, plugin_config in plugin_configs.items():
        for spec in build_plugin_tools(plugin_name, plugin_config):
            tools.register(spec)
    turn_app = TurnApplication(
        model=model,
        tools=tools,
        memory=memory,
        skills=skills,
        sandbox_backend=backend,
        active_sessions=active,
        tool_loop_limit=settings.tool_loop_limit,
        prefer_deferred_tools=settings.prefer_deferred_tools,
        tool_search_mode=settings.tool_search_mode,
        sandbox_mode=settings.sandbox_lifecycle,
        stream_model=settings.stream,
        sandbox_prestart=settings.sandbox_prestart,
        vision_mode=settings.vision,
    )
    return Agent(
        turn_app=turn_app,
        session_id=settings.session_id,
        model=settings.model,
        active_sessions=active,
    )
