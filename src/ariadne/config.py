from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import AriadneError, app_error


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        out[key] = value
    return out


@dataclass(slots=True)
class Settings:
    base_url: str
    api_key: str
    model: str
    workspace: Path
    # Optional read-only host tree mounted at /main-readonly (atelier branch → main workspace).
    main_readonly_workspace: Path | None = None
    session_id: str = "default"
    sandbox: str = "docker"  # docker default (personal Codex-style); local|null escape
    sandbox_lifecycle: str = "per_turn"
    sandbox_profile: str = "minimal"
    sandbox_network: str = "none"  # none | bridge
    sandbox_memory: str = "512m"
    sandbox_cpus: str = "0.5"
    sandbox_pids_limit: int = 128
    sandbox_read_only_rootfs: bool = True
    docker_runtime: str | None = None  # e.g. runsc
    egress_allowed_hosts: str = ""  # comma-separated host allowlist for web_fetch
    egress_default_allow: bool = False
    command_policy_enabled: bool = True
    tool_loop_limit: int = 32
    # Completion budget per model call (not context window). Default 8k; atelier raises to 16k.
    max_tokens: int = 8192
    # Hard prompt-context budget in characters. Required evidence never truncates.
    context_max_chars: int = 120_000
    verbose: bool = False
    json_mode: bool = False
    stream: bool = False
    skills_dir: Path | None = None
    prefer_deferred_tools: bool = True
    toolbox_profile: str = "minimal"
    docker_image: str | None = None
    # hash (default, offline) | openai (explicit opt-in) | auto (openai when creds)
    embedding_provider: str = "hash"
    embedding_model: str = "text-embedding-3-small"
    idle_ttl_seconds: float = 600.0
    max_ttl_seconds: float = 3600.0
    data_dir: Path | None = None
    # When set, load plugins.json from here instead of resolved_data_dir.
    # Web: account dir (survives atelier session data_dir rebinding).
    # CLI: leave None → plugins from resolved_data_dir (+ optional home merge).
    plugins_dir: Path | None = None
    sandbox_prestart: bool = False
    approval_mode: str = "auto"  # auto | on-request | readonly
    merge_home_plugins: bool = True  # CLI: merge ~/.ariadne/plugins.json; web: off
    vision: str = "auto"  # auto | on | off — multimodal image send policy
    # Skill selection budgets (SKILLS G08 / P3 host config)
    skill_auto_load_limit: int = 1
    skill_recommended_limit: int = 5
    skill_auto_body_max: int = 2
    skill_auto_body_chars: int = 6000
    skill_plan_chars: int = 1200
    # Tool deferred search: function (tool_search) | native (auto-materialize) | none
    tool_search_mode: str = "function"
    # L1 summary compressor: grounded | llm
    summary_mode: str = "grounded"
    # Optional host-injected system block (e.g. Atelier KNOWLEDGE.md context)
    extra_system_prompt: str = ""
    # Memory scopes / graded search (design/memory-scopes.md, memory-search.md)
    user_id: str = "local"
    # Cross-workspace user curated root; default ~/.ariadne/memory when None
    user_memory_dir: Path | None = None
    # Default memory_search mode when tool omits mode: auto | fast | deep
    memory_search_mode: str = "auto"
    # deep planner: off | local | llm
    memory_deep_planner: str = "off"
    # L2 projection queue off by default (honest: no silent empty projector)
    enable_memory_projection: bool = False
    # Deterministic-first completed-turn capture. LLM is ambiguity-only.
    memory_auto_capture: bool = True
    memory_auto_capture_llm: bool = True
    memory_episode_search: bool = True
    memory_reflection_sessions: int = 3
    # Optional evidence-quoting LLM verifier for llm_semantic task checks.
    enable_semantic_verifier: bool = False
    # Optional bounded advisory fan-out; delegates never receive capabilities.
    enable_controlled_delegation: bool = False
    # Closed-loop task mode: off | on | auto (resume active task without --task)
    task_mode_policy: str = "auto"

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir is not None:
            return self.data_dir
        return self.workspace / ".ariadne"

    def skill_plan_budgets(self):
        from .skills.store import SkillPlanBudgets

        return SkillPlanBudgets(
            auto_load_limit=max(0, int(self.skill_auto_load_limit)),
            recommended_limit=max(0, int(self.skill_recommended_limit)),
            auto_body_max=max(0, int(self.skill_auto_body_max)),
            auto_body_chars=max(200, int(self.skill_auto_body_chars)),
            plan_chars=max(80, int(self.skill_plan_chars)),
        )


def load_settings(
    *,
    workspace: Path | None = None,
    session_id: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
    sandbox_lifecycle: str | None = None,
    tool_loop_limit: int | None = None,
    verbose: bool = False,
    json_mode: bool = False,
    stream: bool = False,
    skills_dir: Path | None = None,
    prefer_deferred_tools: bool | None = None,
    toolbox_profile: str | None = None,
    docker_image: str | None = None,
    embedding_provider: str | None = None,
    env_file: Path | None = None,
    sandbox_prestart: bool = False,
    force_workspace: bool = False,
    approval_mode: str | None = None,
    vision: str | None = None,
    skill_auto_load_limit: int | None = None,
    skill_recommended_limit: int | None = None,
    skill_auto_body_max: int | None = None,
    skill_auto_body_chars: int | None = None,
    skill_plan_chars: int | None = None,
    tool_search_mode: str | None = None,
    summary_mode: str | None = None,
    task_mode_policy: str | None = None,
    sandbox_profile: str | None = None,
    sandbox_network: str | None = None,
    sandbox_memory: str | None = None,
    sandbox_cpus: str | None = None,
    sandbox_pids_limit: int | None = None,
    docker_runtime: str | None = None,
    egress_allowed_hosts: str | None = None,
    egress_default_allow: bool | None = None,
    command_policy_enabled: bool | None = None,
) -> Settings:
    workspace = (workspace or Path.cwd()).resolve()
    if workspace in {Path("/"), Path.home()} and not force_workspace:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"Refusing risky workspace {workspace} (pass --force-workspace to override)",
            )
        )
    env: dict[str, str] = {}
    # project .env then CWD .env (repo root when developing)
    for candidate in (
        env_file,
        workspace / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ):
        if candidate is None:
            continue
        for k, v in _load_dotenv(Path(candidate)).items():
            env.setdefault(k, v)
    # process env wins
    for k, v in os.environ.items():
        if v is not None:
            env[k] = v

    def pick(*keys: str, default: str = "") -> str:
        for key in keys:
            if key in env and str(env[key]).strip():
                return str(env[key]).strip()
        return default

    base_url = pick("BASE_URL", "OPENAI_BASE_URL").rstrip("/")
    api_key = pick("API_KEY", "OPENAI_API_KEY")
    model_name = model or pick("MODEL", "OPENAI_MODEL", default="grok-4.5")
    sandbox_name = (sandbox or pick("ARIADNE_SANDBOX", default="docker")).strip().lower()
    lifecycle = (
        sandbox_lifecycle or pick("ARIADNE_SANDBOX_LIFECYCLE", default="per_turn")
    ).strip().lower()
    if lifecycle not in {"per_turn", "active_session"}:
        lifecycle = "per_turn"
    sid = session_id or pick("ARIADNE_SESSION", default="")
    if not sid:
        # stable per project (cli-shell-agent §8)
        sid = "local-" + hashlib.sha1(str(workspace).encode()).hexdigest()[:8]
    limit = tool_loop_limit
    if limit is None:
        raw = pick("ARIADNE_TOOL_LOOP_LIMIT", default="32")
        limit = max(int(raw or 32), 1)
    profile = toolbox_profile or pick("ARIADNE_TOOLBOX", default="minimal")
    emb = (
        embedding_provider or pick("ARIADNE_EMBEDDING_PROVIDER", default="hash")
    ).strip().lower()
    if emb not in {"hash", "openai", "auto"}:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown embedding provider: {emb!r} (hash|openai|auto)",
            )
        )
    emb_model = pick("ARIADNE_EMBEDDING_MODEL", default="text-embedding-3-small")
    img = docker_image or pick("ARIADNE_DOCKER_IMAGE", default="") or None
    idle = float(pick("ARIADNE_IDLE_TTL", default="600") or 600)
    maxt = float(pick("ARIADNE_MAX_TTL", default="3600") or 3600)
    stream_flag = stream or pick("ARIADNE_STREAM", default="").lower() in {"1", "true", "yes", "on"}
    prestart_flag = sandbox_prestart or pick("ARIADNE_SANDBOX_PRESTART", default="").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    approval = (approval_mode or pick("ARIADNE_APPROVAL_MODE", default="auto")).strip().lower()
    if approval not in {"auto", "on-request", "readonly"}:
        raise AriadneError(
            app_error("ARIADNE_CONFIG_INVALID", f"unknown approval mode: {approval!r}")
        )
    from .multimodal import normalize_vision_mode

    vision_mode = normalize_vision_mode(vision or pick("ARIADNE_VISION", default="auto"))

    def pick_int(cli: int | None, *keys: str, default: int) -> int:
        if cli is not None:
            return int(cli)
        raw = pick(*keys, default=str(default))
        try:
            return int(raw)
        except ValueError:
            return default

    skill_auto = pick_int(
        skill_auto_load_limit, "ARIADNE_SKILL_AUTO_LOAD_LIMIT", default=1
    )
    skill_rec = pick_int(
        skill_recommended_limit, "ARIADNE_SKILL_RECOMMENDED_LIMIT", default=5
    )
    skill_bodies = pick_int(
        skill_auto_body_max, "ARIADNE_SKILL_AUTO_BODY_MAX", default=2
    )
    skill_body_chars = pick_int(
        skill_auto_body_chars, "ARIADNE_SKILL_AUTO_BODY_CHARS", default=6000
    )
    skill_plan = pick_int(skill_plan_chars, "ARIADNE_SKILL_PLAN_CHARS", default=1200)

    search_mode = (
        tool_search_mode or pick("ARIADNE_TOOL_SEARCH_MODE", default="function")
    ).strip().lower()
    if search_mode not in {"function", "native", "none"}:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown tool search mode: {search_mode!r} (function|native|none)",
            )
        )
    sum_mode = (
        summary_mode or pick("ARIADNE_SUMMARY_MODE", default="grounded")
    ).strip().lower()
    if sum_mode not in {"grounded", "llm"}:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown summary mode: {sum_mode!r} (grounded|llm)",
            )
        )

    max_tok = pick_int(None, "ARIADNE_MAX_TOKENS", default=8192)
    if max_tok < 256:
        max_tok = 256
    if max_tok > 128_000:
        max_tok = 128_000

    task_pol = (
        task_mode_policy or pick("ARIADNE_TASK_MODE_POLICY", default="auto") or "auto"
    ).strip().lower()
    if task_pol not in {"off", "on", "auto"}:
        raise AriadneError(
            app_error(
                "ARIADNE_CONFIG_INVALID",
                f"unknown task_mode_policy: {task_pol!r} (off|on|auto)",
            )
        )

    return Settings(
        base_url=base_url,
        api_key=api_key,
        model=model_name,
        workspace=workspace,
        session_id=sid,
        sandbox=sandbox_name,
        sandbox_lifecycle=lifecycle,
        tool_loop_limit=limit,
        max_tokens=max_tok,
        verbose=verbose,
        json_mode=json_mode,
        stream=stream_flag,
        skills_dir=skills_dir,
        prefer_deferred_tools=True if prefer_deferred_tools is None else prefer_deferred_tools,
        toolbox_profile=profile,
        docker_image=img,
        embedding_provider=emb,
        embedding_model=emb_model,
        idle_ttl_seconds=idle,
        max_ttl_seconds=maxt,
        sandbox_prestart=prestart_flag,
        approval_mode=approval,
        vision=vision_mode,
        skill_auto_load_limit=skill_auto,
        skill_recommended_limit=skill_rec,
        skill_auto_body_max=skill_bodies,
        skill_auto_body_chars=skill_body_chars,
        skill_plan_chars=skill_plan,
        tool_search_mode=search_mode,
        summary_mode=sum_mode,
        user_id=(pick("ARIADNE_USER_ID", default="local") or "local").strip() or "local",
        memory_search_mode=(
            pick("ARIADNE_MEMORY_SEARCH_MODE", default="auto") or "auto"
        ).strip().lower(),
        memory_deep_planner=(
            pick("ARIADNE_MEMORY_DEEP_PLANNER", default="off") or "off"
        ).strip().lower(),
        enable_memory_projection=pick(
            "ARIADNE_ENABLE_MEMORY_PROJECTION", default="0"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        memory_auto_capture=pick(
            "ARIADNE_MEMORY_AUTO_CAPTURE", default="1"
        ).strip().lower()
        not in {"0", "false", "no", "off"},
        memory_auto_capture_llm=pick(
            "ARIADNE_MEMORY_AUTO_CAPTURE_LLM", default="1"
        ).strip().lower()
        not in {"0", "false", "no", "off"},
        memory_episode_search=pick(
            "ARIADNE_MEMORY_EPISODE_SEARCH", default="1"
        ).strip().lower()
        not in {"0", "false", "no", "off"},
        memory_reflection_sessions=max(
            2,
            pick_int(None, "ARIADNE_MEMORY_REFLECTION_SESSIONS", default=3),
        ),
        enable_semantic_verifier=pick(
            "ARIADNE_ENABLE_SEMANTIC_VERIFIER", default="0"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        enable_controlled_delegation=pick(
            "ARIADNE_ENABLE_CONTROLLED_DELEGATION", default="0"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        task_mode_policy=task_pol,
        sandbox_profile=(
            sandbox_profile or pick("ARIADNE_SANDBOX_PROFILE", default="minimal")
        ).strip().lower(),
        sandbox_network=(
            sandbox_network or pick("ARIADNE_SANDBOX_NETWORK", default="none")
        ).strip().lower(),
        sandbox_memory=(sandbox_memory or pick("ARIADNE_SANDBOX_MEMORY", default="512m")).strip(),
        sandbox_cpus=(sandbox_cpus or pick("ARIADNE_SANDBOX_CPU", default="0.5")).strip(),
        sandbox_pids_limit=int(
            sandbox_pids_limit
            if sandbox_pids_limit is not None
            else pick("ARIADNE_SANDBOX_PIDS", default="128") or 128
        ),
        docker_runtime=(
            docker_runtime
            if docker_runtime is not None
            else (pick("ARIADNE_DOCKER_RUNTIME", default="") or None)
        ),
        egress_allowed_hosts=(
            egress_allowed_hosts
            if egress_allowed_hosts is not None
            else pick("ARIADNE_EGRESS_ALLOWED", default="")
        ),
        egress_default_allow=(
            egress_default_allow
            if egress_default_allow is not None
            else pick("ARIADNE_EGRESS_DEFAULT_ALLOW", default="").lower()
            in {"1", "true", "yes", "on"}
        ),
        command_policy_enabled=(
            command_policy_enabled
            if command_policy_enabled is not None
            else pick("ARIADNE_COMMAND_POLICY", default="1").lower()
            not in {"0", "false", "no", "off"}
        ),
    )
