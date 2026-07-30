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
from ..sandbox.docker_check import check_docker, image_present
from ..sandbox.docker_config import DockerSandboxConfig
from ..sandbox.local import LocalWorkdirSandbox
from ..sandbox.null import NullSandbox
from ..sandbox.policy import CommandPolicy, EgressPolicy
from ..sandbox.profiles import get_profile as get_sandbox_profile
from ..sandbox.profiles import resolve_image
from ..sandbox.runtime_agent import RuntimeAgent
from ..sandbox.toolbox import get_profile as get_toolbox_profile
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

    sb_profile = get_sandbox_profile(getattr(settings, "sandbox_profile", None) or "minimal")
    docker_image = resolve_image(
        profile=sb_profile.name,
        docker_image=settings.docker_image,
    )
    # If official tag missing, fall back to public slim so first-run still works.
    if settings.sandbox == "docker" and not image_present(docker_image):
        from ..sandbox.profiles import PUBLIC_FALLBACK

        if docker_image != PUBLIC_FALLBACK and image_present(PUBLIC_FALLBACK):
            docker_image = PUBLIC_FALLBACK
        # else leave tag; docker pull may still work on start

    network = (getattr(settings, "sandbox_network", None) or "none").strip().lower()
    if network not in {"none", "bridge"}:
        network = "none"

    main_ro = getattr(settings, "main_readonly_workspace", None)
    if main_ro is not None:
        main_ro = Path(main_ro)

    if settings.sandbox in {"local", "workdir", "localworkdir"}:
        backend = LocalWorkdirSandbox(
            workspace=settings.workspace,
            data_dir=data_dir,
            main_readonly=main_ro,
        )
        backend_name = "local"
    elif settings.sandbox in {"null", "none", "off"}:
        backend = NullSandbox()
        backend_name = "null"
    elif settings.sandbox == "docker":
        chk = check_docker()
        if not chk.ok:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", chk.detail))
        resources = sb_profile.resources
        mem = getattr(settings, "sandbox_memory", None) or resources.memory
        cpus = getattr(settings, "sandbox_cpus", None) or resources.cpus
        pids = int(getattr(settings, "sandbox_pids_limit", None) or resources.pids_limit)
        runtime = getattr(settings, "docker_runtime", None) or None
        cfg = DockerSandboxConfig(
            image=docker_image,
            network=network,
            memory=str(mem),
            cpus=str(cpus),
            pids_limit=pids,
            runtime=runtime,
            read_only_rootfs=bool(getattr(settings, "sandbox_read_only_rootfs", True)),
        )
        backend = DockerSandbox(
            workspace=settings.workspace,
            data_dir=data_dir,
            config=cfg,
            require_daemon=True,
            main_readonly=main_ro,
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

    # Default hash (offline). openai requires explicit opt-in; auto only when set.
    emb_pref = (getattr(settings, "embedding_provider", None) or "hash").strip().lower()
    if emb_pref == "auto":
        if settings.api_key and settings.base_url:
            emb_pref = "openai"
        else:
            emb_pref = "hash"
    if emb_pref == "openai":
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
    # User-scope curated + episodic under ~/.ariadne (CLI) or account memory (Web).
    user_memory_dir = getattr(settings, "user_memory_dir", None)
    if user_memory_dir is None:
        user_memory_dir = Path.home() / ".ariadne" / "memory"
    else:
        user_memory_dir = Path(user_memory_dir)
    user_memory_dir.mkdir(parents=True, exist_ok=True)
    (user_memory_dir / "episodic").mkdir(parents=True, exist_ok=True)
    enable_projection = bool(getattr(settings, "enable_memory_projection", False))
    projection = None
    if enable_projection:
        projection = ProjectionWorker(
            path=data_dir / "memory" / "projection_jobs.json",
            state_store=state_store,
        )
    deep_planner = None
    deep_mode = (getattr(settings, "memory_deep_planner", None) or "off").strip().lower()
    if deep_mode == "llm":
        from ..memory.deep_planner import make_llm_deep_planner

        deep_planner = make_llm_deep_planner(model)
    elif deep_mode == "local":
        from ..memory.deep_planner import LocalSplitPlanner

        deep_planner = LocalSplitPlanner()
    memory = MemoryFacade(
        transcript=transcript,
        curated=CuratedStore(path=data_dir / "memory" / "curated.json"),
        state=state_store,
        summaries=summary_store,
        semantic=SemanticIndex(
            path=data_dir / "memory" / "semantic.json",
            embedder=embedder,
            embedding_model_id=(
                f"openai:{settings.embedding_model}"
                if emb_pref == "openai"
                else "hash:64"
            ),
        ),
        projection=projection,
        hybrid_semantic=True,
        user_id=getattr(settings, "user_id", None) or "local",
        user_curated=CuratedStore(path=user_memory_dir / "curated.json"),
        user_episodic=SemanticIndex(
            path=user_memory_dir / "episodic" / "semantic.json",
            embedder=embedder,
            embedding_model_id=(
                f"openai:{settings.embedding_model}"
                if emb_pref == "openai"
                else "hash:64"
            ),
        ),
        deep_planner=deep_planner,
        search_mode_default=getattr(settings, "memory_search_mode", None) or "auto",
        workspace_key=str(settings.workspace.resolve()) if settings.workspace else "",
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

    plugin_configs: dict[str, dict[str, str]] = {}
    store_paths = []
    if settings.merge_home_plugins:
        store_paths.append(Path.home() / ".ariadne" / "plugins.json")
    # plugins_dir keeps account plugins when data_dir is rebound (e.g. atelier scope).
    plugin_root = getattr(settings, "plugins_dir", None) or settings.resolved_data_dir
    store_paths.append(Path(plugin_root) / "plugins.json")
    for store_path in store_paths:
        plugin_configs.update(PluginStore(store_path).enabled())
    for plugin_name, plugin_config in plugin_configs.items():
        for spec in build_plugin_tools(plugin_name, plugin_config):
            tools.register(spec)

    # In-process RuntimeAgent (command policy + host egress policy)
    allowed = tuple(
        h.strip()
        for h in (getattr(settings, "egress_allowed_hosts", None) or "").split(",")
        if h.strip()
    )
    egress = EgressPolicy(
        allowed_hosts=allowed,
        default_allow=bool(getattr(settings, "egress_default_allow", False)),
    )
    cmd_policy = CommandPolicy(
        audit_path=data_dir / "audit" / "sandbox_commands.jsonl",
        enabled=bool(getattr(settings, "command_policy_enabled", True)),
    )
    runtime = RuntimeAgent(command_policy=cmd_policy, egress_policy=egress)

    # Keep toolbox profile import used for doctor/hints (side-effect free)
    _ = get_toolbox_profile(settings.toolbox_profile)

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
        runtime_agent=runtime,
        extra_system_prompt=getattr(settings, "extra_system_prompt", "") or "",
        max_tokens=int(getattr(settings, "max_tokens", None) or 8192),
    )
    return Agent(
        turn_app=turn_app,
        session_id=settings.session_id,
        model=settings.model,
        active_sessions=active,
    )
