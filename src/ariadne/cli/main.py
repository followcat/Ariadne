from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from .. import __version__
from ..config import load_settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from ..sandbox.toolbox import list_profiles, profile_as_dict
from ..types import TurnEvent
from .render import render_json
from . import ui

# Subcommands recognized before free-form prompt (codex-style bare entry).
_KNOWN_COMMANDS = frozenset(
    {
        "run",
        "exec",
        "chat",
        "resume",
        "doctor",
        "tools",
        "skills",
        "sessions",
        "plugins",
        "plugin",
        "serve",
        "toolbox",
        "memory-worker",
        "atelier",
        "version",
        "help",
    }
)

# Long options that consume the next argv token as a value.
_VALUE_OPTIONS = frozenset(
    {
        "--session",
        "--workspace",
        "--sandbox",
        "--sandbox-lifecycle",
        "--toolbox",
        "--docker-image",
        "--model",
        "--tool-loop-limit",
        "--skills-dir",
        "--approval-mode",
        "--host",
        "--port",
    }
)


def extract_free_prompt(argv: list[str]) -> tuple[list[str], str | None]:
    """Split ``[flags…] [PROMPT…]`` when the first positional is not a command.

    Returns ``(argv_for_argparse, free_prompt)``. If the first positional is a
    known subcommand, argv is unchanged and free_prompt is None.
    """
    prefix: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest = " ".join(argv[i + 1 :]).strip()
            return prefix, rest or None
        if tok.startswith("-"):
            prefix.append(tok)
            key = tok.split("=", 1)[0]
            if key in _VALUE_OPTIONS and "=" not in tok:
                i += 1
                if i < len(argv):
                    prefix.append(argv[i])
            i += 1
            continue
        # first positional token
        if tok in _KNOWN_COMMANDS:
            return list(argv), None
        prompt = " ".join(argv[i:]).strip()
        return prefix, prompt or None
    return prefix, None


def _add_global_flags(p: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Global flags. suppress=True lets subcommands accept them after the
    subcommand name without clobbering values given before it."""
    s = argparse.SUPPRESS if suppress else None
    b = argparse.SUPPRESS if suppress else False
    p.add_argument("--session", default=s, help="Session id (default: local-<workspace hash>)")
    p.add_argument("--workspace", type=Path, default=s, help="Project workspace (default: cwd)")
    p.add_argument(
        "--sandbox",
        choices=["local", "null", "docker"],
        default=s,
        help="Sandbox backend",
    )
    p.add_argument(
        "--no-sandbox",
        action="store_true",
        default=b,
        help="Force NullSandbox (alias for --sandbox null)",
    )
    p.add_argument(
        "--sandbox-lifecycle",
        choices=["per_turn", "active_session"],
        default=s,
        help="Sandbox lifecycle (chat prefers active_session)",
    )
    p.add_argument("--toolbox", default=s, help="Toolbox profile: minimal|docs|data")
    p.add_argument("--docker-image", default=s, help="Override docker image")
    p.add_argument("--model", default=s, help="Override MODEL from .env")
    p.add_argument("--tool-loop-limit", type=int, default=s)
    p.add_argument("--skills-dir", type=Path, default=s, help="Extra skills directory")
    p.add_argument(
        "--eager-tools", action="store_true", default=b, help="Send all tool schemas eagerly"
    )
    p.add_argument(
        "--tool-search-mode",
        choices=["function", "native", "none"],
        default=s,
        help="Deferred tool load: function (tool_search), native (auto-load), none",
    )
    p.add_argument(
        "--summary-mode",
        choices=["grounded", "llm"],
        default=s,
        help="L1 summary compressor: grounded extract or llm (falls back to grounded)",
    )
    p.add_argument(
        "--skill-auto-load",
        type=int,
        default=s,
        dest="skill_auto_load_limit",
        help="Max auto_load skills per turn (SkillPlanBudgets)",
    )
    p.add_argument(
        "--skill-recommended",
        type=int,
        default=s,
        dest="skill_recommended_limit",
        help="Max recommended skills in plan",
    )
    p.add_argument(
        "--skill-body-max",
        type=int,
        default=s,
        dest="skill_auto_body_max",
        help="Max auto_load skill bodies injected per turn",
    )
    p.add_argument(
        "--skill-body-chars",
        type=int,
        default=s,
        dest="skill_auto_body_chars",
        help="Max chars per auto_load skill body",
    )
    p.add_argument(
        "--skill-plan-chars",
        type=int,
        default=s,
        dest="skill_plan_chars",
        help="Max chars for [SKILL_SELECTION] plan block",
    )
    p.add_argument(
        "--force-workspace",
        action="store_true",
        default=b,
        help="Allow risky workspaces like / or $HOME",
    )
    p.add_argument("-v", "--verbose", action="store_true", default=b, help="Show tool traces and usage")
    p.add_argument("--json", action="store_true", default=b, dest="json_mode", help="Print TurnResult JSON")
    p.add_argument("--stream", action="store_true", default=b, help="Stream model tokens / turn events")
    p.add_argument(
        "--sandbox-prestart",
        action="store_true",
        default=b,
        help="Start sandbox session in parallel with memory context build",
    )
    p.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        default=b,
        help="Continue the most recent session",
    )
    p.add_argument(
        "--approval-mode",
        choices=["auto", "on-request", "readonly"],
        default=s,
        help="Tool approval policy (default: auto)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description=(
            "Ariadne — personal shell agent. "
            "With no subcommand, starts the interactive CLI (codex-style). "
            "Pass an optional prompt to seed the first turn."
        ),
        epilog="Examples:\n  ariadne\n  ariadne \"summarize this repo\"\n  ariadne run \"…\"\n  ariadne exec \"…\"",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"ariadne {__version__}")
    _add_global_flags(parser, suppress=False)
    # Interactive defaults (also usable with bare entry / chat)
    parser.add_argument(
        "--no-welcome",
        action="store_true",
        default=False,
        help="Suppress interactive welcome banner",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        default=False,
        help="Disable streaming in interactive mode",
    )

    sub = parser.add_subparsers(dest="command", required=False)

    run_p = sub.add_parser(
        "run",
        aliases=["exec"],
        help="Run one agent turn non-interactively (alias: exec)",
    )
    _add_global_flags(run_p, suppress=True)
    run_p.add_argument("prompt", nargs="+", help="User prompt")

    chat_p = sub.add_parser("chat", help="Interactive multi-turn shell agent (default entry alias)")
    _add_global_flags(chat_p, suppress=True)
    chat_p.add_argument("--no-welcome", action="store_true", default=False, help=argparse.SUPPRESS)
    chat_p.add_argument("--no-stream", action="store_true", default=False, help=argparse.SUPPRESS)

    resume_p = sub.add_parser("resume", help="Resume a session in interactive mode")
    _add_global_flags(resume_p, suppress=True)
    resume_p.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="Session id to resume (omit to list sessions)",
    )
    resume_p.add_argument(
        "--last",
        action="store_true",
        help="Resume the most recent session",
    )
    resume_p.add_argument("--no-welcome", action="store_true", default=False, help=argparse.SUPPRESS)
    resume_p.add_argument("--no-stream", action="store_true", default=False, help=argparse.SUPPRESS)

    sub.add_parser("doctor", help="Check configuration")
    sub.add_parser("tools", help="List tools")
    skills_p = sub.add_parser("skills", help="List installed skills")
    skills_p.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "validate"],
        help="list (default) or validate skill packs strictly",
    )
    sub.add_parser("sessions", help="List recorded sessions")
    sub.add_parser("plugins", help="List official plugins and their status")
    plugin_p = sub.add_parser("plugin", help="Enable/disable an official plugin")
    plugin_p.add_argument("action", choices=["enable", "disable"])
    plugin_p.add_argument("name", help="Plugin name: odoo | gitlab | redmine")
    plugin_p.add_argument("--url", default=None, help="Base URL of the service")
    plugin_p.add_argument("--token", default=None, help="API token (gitlab)")
    plugin_p.add_argument("--api-key", default=None, help="API key (redmine)")
    plugin_p.add_argument("--database", default=None, help="Odoo database")
    plugin_p.add_argument("--login", default=None, help="Odoo login")
    plugin_p.add_argument("--password", default=None, help="Odoo password/API key")
    plugin_p.add_argument(
        "--workspace-scope",
        action="store_true",
        help="Store config at workspace level instead of user level (~/.ariadne)",
    )
    serve_p = sub.add_parser("serve", help="Start the web UI (FastAPI + SSE)")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=8420)
    sub.add_parser("toolbox", help="List toolbox profiles")
    sub.add_parser("version", help="Print version")
    mem_w = sub.add_parser(
        "memory-worker",
        help="Drain pending turn summaries and projection jobs (in-process)",
    )
    mem_w.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Process one batch and exit (default)",
    )
    mem_w.add_argument(
        "--loop",
        action="store_true",
        default=False,
        help="Keep draining on an interval until idle (with --stop-when-idle) or forever",
    )
    mem_w.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Seconds between loop ticks (default 2)",
    )
    mem_w.add_argument(
        "--stop-when-idle",
        action="store_true",
        default=False,
        help="Exit the loop when a tick finds no pending work",
    )
    mem_w.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Cap loop iterations (default unlimited with --loop)",
    )
    mem_w.add_argument(
        "--subprocess",
        action="store_true",
        default=False,
        help="Spawn out-of-process worker (python -m ariadne.memory.worker_main)",
    )
    mem_w.add_argument(
        "--consolidate",
        action="store_true",
        default=False,
        help="Propose L3 curated consolidation from session signals (dry-run unless --apply)",
    )
    mem_w.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="With --consolidate: write proposed entries to curated user scope",
    )
    mem_w.add_argument(
        "--text",
        action="append",
        default=None,
        help="With --consolidate: extra signal text (repeatable)",
    )
    # ── Atelier (project workshop) ──
    at = sub.add_parser("atelier", help="Project workshop (workspace + knowledge + branch sessions)")
    at_sub = at.add_subparsers(dest="atelier_cmd")
    at_create = at_sub.add_parser("create", help="Create an atelier")
    at_create.add_argument("name", help="Atelier id/name (slug)")
    at_create.add_argument("--from", dest="from_path", default=None, help="Use existing code dir as workspace")
    at_create.add_argument("--no-scan", action="store_true", help="Skip knowledge scan")
    at_sub.add_parser("list", help="List ateliers")
    at_open = at_sub.add_parser("open", help="Open atelier REPL (main or --session)")
    at_open.add_argument("name")
    at_open.add_argument("--session", "-s", default=None, help="Session id (default main)")
    at_del = at_sub.add_parser("delete", help="Delete atelier")
    at_del.add_argument("name")
    at_del.add_argument("-y", "--yes", action="store_true")
    at_br = at_sub.add_parser("branch", help="Branch sessions")
    at_br_sub = at_br.add_subparsers(dest="branch_cmd")
    at_br_c = at_br_sub.add_parser("create")
    at_br_c.add_argument("project")
    at_br_c.add_argument("branch_name")
    at_br_l = at_br_sub.add_parser("list")
    at_br_l.add_argument("project")
    at_br_m = at_br_sub.add_parser("merge")
    at_br_m.add_argument("project")
    at_br_m.add_argument("branch_name")
    at_br_d = at_br_sub.add_parser("discard")
    at_br_d.add_argument("project")
    at_br_d.add_argument("branch_name")
    at_kn = at_sub.add_parser("knowledge", help="KNOWLEDGE.md")
    at_kn_sub = at_kn.add_subparsers(dest="knowledge_cmd")
    for kn in ("show", "edit", "refresh", "history"):
        p = at_kn_sub.add_parser(kn)
        p.add_argument("project")
    return parser


def _settings_from_args(args: argparse.Namespace, *, default_lifecycle: str | None = None):
    lifecycle = args.sandbox_lifecycle
    if lifecycle is None and default_lifecycle is not None:
        lifecycle = default_lifecycle
    sandbox = "null" if getattr(args, "no_sandbox", False) else args.sandbox
    return load_settings(
        workspace=args.workspace,
        session_id=args.session,
        model=args.model,
        sandbox=sandbox,
        sandbox_lifecycle=lifecycle,
        tool_loop_limit=args.tool_loop_limit,
        verbose=args.verbose,
        json_mode=args.json_mode,
        stream=args.stream,
        skills_dir=args.skills_dir,
        prefer_deferred_tools=not args.eager_tools,
        toolbox_profile=args.toolbox,
        docker_image=args.docker_image,
        sandbox_prestart=args.sandbox_prestart,
        force_workspace=getattr(args, "force_workspace", False),
        approval_mode=getattr(args, "approval_mode", None),
        skill_auto_load_limit=getattr(args, "skill_auto_load_limit", None),
        skill_recommended_limit=getattr(args, "skill_recommended_limit", None),
        skill_auto_body_max=getattr(args, "skill_auto_body_max", None),
        skill_auto_body_chars=getattr(args, "skill_auto_body_chars", None),
        skill_plan_chars=getattr(args, "skill_plan_chars", None),
        tool_search_mode=getattr(args, "tool_search_mode", None),
        summary_mode=getattr(args, "summary_mode", None),
    )


def _apply_continue(args: argparse.Namespace, settings) -> None:
    if getattr(args, "continue_last", False):
        from .sessions import most_recent

        recent = most_recent(settings.resolved_data_dir)
        if recent:
            settings.session_id = recent


def _compose_with_approval(settings):
    from .approval import make_approval_hook
    from .grants import GrantStore

    agent = compose_agent(settings)
    grant_path = settings.resolved_data_dir / "grants.json"
    agent.turn_app.approval_hook = make_approval_hook(
        settings.approval_mode,
        grant_store=GrantStore(path=grant_path),
        session_id=settings.session_id,
    )
    return agent


def _event_to_jsonable(event: TurnEvent) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in event.data.items():
        if key == "result":
            continue
        data[key] = value
    return {"kind": event.kind, "data": data}


async def _emit_stream(
    agent, prompt: str, *, json_mode: bool, verbose: bool, images: list | None = None
) -> tuple[int, object]:
    final = None
    saw_delta = False
    async for event in agent.run_stream(prompt, images=images):
        if event.kind in {"turn_completed", "turn_failed"}:
            final = event.data.get("result")
            continue
        if json_mode:
            # NDJSON event stream (cli-shell-agent §6.2), final result after
            sys.stdout.write(json.dumps(_event_to_jsonable(event), ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
            continue
        if event.kind == "model_thinking_delta":
            # Dim thinking stream; hosts may suppress. Web collapses after answer.
            if verbose:
                ui.print_delta(str(event.data.get("text") or ""))
        elif event.kind == "model_delta":
            saw_delta = True
            ui.print_delta(str(event.data.get("text") or ""))
        elif event.kind == "tool_started":
            ui.print_tool_start(str(event.data.get("name") or ""), {})
        elif event.kind == "tool_completed":
            ui.print_tool_done(
                str(event.data.get("name") or ""),
                str(event.data.get("status") or ""),
                event.data.get("output"),
            )
        elif event.kind == "guard_finding":
            ui.console.print(
                f"[yellow]guard ({event.data.get('direction')})[/] {event.data.get('detail')}"
            )
        elif verbose and event.kind == "turn_started":
            ui.print_event_line("turn", str(event.data.get("turn_id") or ""))
        elif verbose and event.kind in {"skill_event", "memory_layer"}:
            detail = event.data.get("detail") or event.data.get("name") or ""
            ui.print_event_line(str(event.kind), str(detail))
    if final is None:
        ui.print_error("STREAM", "stream ended without result")
        return 1, None
    if json_mode:
        sys.stdout.write(render_json(final))
        return (0 if final.status == "completed" else 1), final
    if final.status != "completed":
        ui.render_result(final, verbose=True, skip_text=True)
    else:
        if final.text and not saw_delta:
            # provider returned no deltas — print the final text now
            ui.print_assistant(final.text)
        elif final.text and not final.text.endswith("\n"):
            ui.out.print()
        ui.render_result(final, verbose=verbose, skip_text=True)
    return (0 if final.status == "completed" else 1), final


async def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    _apply_continue(args, settings)
    agent = _compose_with_approval(settings)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Prompt is empty.", file=sys.stderr)
        return 2
    if settings.stream:
        code, _ = await _emit_stream(
            agent, prompt, json_mode=settings.json_mode, verbose=settings.verbose
        )
        return code
    result = await agent.run(prompt)
    if settings.json_mode:
        sys.stdout.write(render_json(result))
    else:
        ui.render_result(result, verbose=settings.verbose)
    return 0 if result.status == "completed" else 1


def cmd_interactive(
    args: argparse.Namespace,
    *,
    initial_prompt: str | None = None,
    force_session: str | None = None,
) -> int:
    """Interactive REPL (default bare-entry path; also used by chat/resume)."""
    settings = _settings_from_args(args, default_lifecycle="active_session")
    _apply_continue(args, settings)
    if force_session:
        settings.session_id = force_session
    agent = _compose_with_approval(settings)
    from .repl import run_repl

    return run_repl(
        args,
        settings,
        agent,
        welcome=not getattr(args, "no_welcome", False),
        initial_prompt=initial_prompt,
    )


def cmd_chat(args: argparse.Namespace) -> int:
    return cmd_interactive(args)


def cmd_resume(args: argparse.Namespace) -> int:
    from .sessions import list_sessions, most_recent

    settings = _settings_from_args(args, default_lifecycle="active_session")
    data = settings.resolved_data_dir
    if getattr(args, "last", False) or getattr(args, "continue_last", False):
        recent = most_recent(data)
        if not recent:
            print("No sessions recorded.", file=sys.stderr)
            return 1
        return cmd_interactive(args, force_session=recent)
    sid = getattr(args, "session_id", None)
    if not sid:
        # list only (picker-style listing without TUI)
        return cmd_sessions(args)
    known = {s.session_id for s in list_sessions(data)}
    if sid not in known:
        print(f"Unknown session: {sid}", file=sys.stderr)
        return 1
    return cmd_interactive(args, force_session=sid)


async def cmd_doctor(args: argparse.Namespace) -> int:
    from ..sandbox.docker_check import check_docker, image_present
    from ..sandbox.profiles import resolve_image

    settings = _settings_from_args(args)
    print(f"workspace:  {settings.workspace}")
    print(f"session:    {settings.session_id}")
    print(f"sandbox:    {settings.sandbox}")
    print(f"lifecycle:  {settings.sandbox_lifecycle}")
    print(f"sb_profile: {settings.sandbox_profile}")
    print(f"sb_network: {settings.sandbox_network}")
    print(f"toolbox:    {settings.toolbox_profile}")
    img = resolve_image(profile=settings.sandbox_profile, docker_image=settings.docker_image)
    print(f"image:      {img}")
    if settings.sandbox == "docker":
        chk = check_docker()
        print(f"docker:     {'OK' if chk.ok else 'FAIL'} — {chk.detail}")
        print(f"image_local:{'yes' if image_present(img) else 'no (will try pull/public fallback)'}")
    print(f"model:      {settings.model}")
    print(f"base_url:   {'set' if settings.base_url else 'MISSING'}")
    print(f"api_key:    {'set' if settings.api_key else 'MISSING'}")
    print(f"data_dir:   {settings.resolved_data_dir}")
    print(f"deferred:   {settings.prefer_deferred_tools}")
    print(f"tool_search:{settings.tool_search_mode}")
    print(f"summary:    {settings.summary_mode}")
    print(f"egress:     allowed_hosts={settings.egress_allowed_hosts or '(none)'} default_allow={settings.egress_default_allow}")
    b = settings.skill_plan_budgets()
    print(
        f"skill_plan: auto={b.auto_load_limit} rec={b.recommended_limit} "
        f"bodies={b.auto_body_max}x{b.auto_body_chars} plan_chars={b.plan_chars}"
    )
    print(f"stream:     {settings.stream}")
    print(f"embeddings: {settings.embedding_provider}")
    if not settings.base_url or not settings.api_key:
        print("status: incomplete config", file=sys.stderr)
        return 1
    print("status: ok")
    return 0


async def cmd_tools(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    # tools listing should not require model credentials
    from ..memory.curated import CuratedStore
    from ..memory.facade import MemoryFacade
    from ..memory.semantic import SemanticIndex
    from ..memory.state import ConversationStateStore
    from ..memory.summary import TurnSummaryStore
    from ..memory.transcript import TranscriptStore
    from ..skills.store import SkillStore
    from ..tools.registry import build_default_registry

    data = settings.resolved_data_dir
    memory = MemoryFacade(
        transcript=TranscriptStore(path=data / "sessions" / "doctor.jsonl"),
        curated=CuratedStore(path=data / "memory" / "curated.json"),
        state=ConversationStateStore(path=data / "memory" / "state.json"),
        summaries=TurnSummaryStore(path=data / "memory" / "summaries.json"),
        semantic=SemanticIndex(path=data / "memory" / "semantic.json"),
    )
    skills = SkillStore({})
    registry = build_default_registry(memory=memory, skills=skills, enable_deferred_demo=True)
    from ..plugins import PluginStore, build_plugin_tools

    for plugin_name, plugin_config in PluginStore(data / "plugins.json").enabled().items():
        for spec in build_plugin_tools(plugin_name, plugin_config):
            registry.register(spec)
    for name, spec in sorted(registry.tools.items()):
        print(f"{name}\t{spec.tool_exposure}\t{spec.catalog_description or spec.description}")
    return 0


async def cmd_skills(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    skill_dirs: list[Path] = []
    skill_namespaces: list[str] = []
    repo_root = Path(__file__).resolve().parents[3]
    user_skills = settings.resolved_data_dir / "skills" / "user"
    for candidate, ns in (
        (repo_root / "skills" / "builtin", "builtin"),
        (settings.workspace / "skills", "workspace"),
        (settings.skills_dir, "local"),
        (user_skills, "user"),
    ):
        if candidate is not None and Path(candidate).is_dir():
            skill_dirs.append(Path(candidate))
            skill_namespaces.append(ns)
    from ..skills.store import SkillStore

    if args.action == "validate":
        store = SkillStore.from_dirs(
            skill_dirs, strict=True, user_root=user_skills, namespaces=skill_namespaces
        )
        for skill in store.list():
            print(f"ok\t{skill.name}\t{skill.namespace}")
        print(f"valid: {len(store.list())} skill(s)")
        return 0

    store = SkillStore.from_dirs(
        skill_dirs, strict=False, user_root=user_skills, namespaces=skill_namespaces
    )
    for skill in store.list():
        print(f"{skill.name}\t{skill.namespace}\t{skill.description}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from ..web.app import create_app

    settings = _settings_from_args(args)
    app = create_app(settings)
    ui.print_info(f"Ariadne web UI: http://{args.host}:{args.port}")
    ui.print_info(
        f"workspace: {settings.workspace}  "
        f"(open folder shared by chats; 作坊/旁支 override — docs/design/web-workspace.md)"
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def _plugin_store(args: argparse.Namespace, settings) -> "object":
    from pathlib import Path as _P

    from ..plugins import PluginStore

    if getattr(args, "workspace_scope", False):
        return PluginStore(settings.resolved_data_dir / "plugins.json")
    return PluginStore(_P.home() / ".ariadne" / "plugins.json")


def cmd_plugins(args: argparse.Namespace) -> int:
    from pathlib import Path as _P

    from ..plugins import PLUGIN_REGISTRY, PluginStore

    settings = _settings_from_args(args)
    # Merge user-level then workspace-level (workspace wins on name clash).
    configured: dict = {}
    for candidate in (
        _P.home() / ".ariadne" / "plugins.json",
        settings.resolved_data_dir / "plugins.json",
    ):
        for name, entry in PluginStore(candidate).list().items():
            configured[name] = entry
    rows = []
    for name, plugin in sorted(PLUGIN_REGISTRY.items()):
        entry = configured.get(name) or {}
        status = "enabled" if entry.get("enabled") else ("disabled" if entry else "not configured")
        rows.append([name, status, plugin.description])
    ui.print_table(["plugin", "status", "description"], rows)
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    from ..plugins import PLUGIN_REGISTRY

    settings = _settings_from_args(args)
    store = _plugin_store(args, settings)
    if args.name not in PLUGIN_REGISTRY:
        ui.print_error("PLUGIN", f"unknown plugin: {args.name} (see: ariadne plugins)")
        return 2
    if args.action == "disable":
        store.disable(args.name)
        ui.print_info(f"plugin {args.name} disabled")
        return 0
    config = {
        k: v
        for k, v in {
            "url": args.url,
            "token": args.token,
            "api_key": args.api_key,
            "database": args.database,
            "login": args.login,
            "password": args.password,
        }.items()
        if v
    }
    required = PLUGIN_REGISTRY[args.name].required_config
    missing = [k for k in required if k not in config]
    if missing:
        ui.print_error("PLUGIN", f"missing required options for {args.name}: {', '.join(missing)}")
        return 2
    store.enable(args.name, config)
    ui.print_info(f"plugin {args.name} enabled (config stored in {store.path})")
    return 0


def cmd_sessions(args: argparse.Namespace) -> int:
    from datetime import datetime

    from .sessions import list_sessions

    settings = _settings_from_args(args)
    sessions = list_sessions(settings.resolved_data_dir)
    if not sessions:
        ui.print_info("no sessions recorded")
        return 0
    ui.print_table(
        ["session", "turns", "updated"],
        [
            [s.session_id, str(s.turns), datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")]
            for s in sessions
        ],
    )
    return 0


async def cmd_memory_worker(args: argparse.Namespace) -> int:
    from ..memory.consolidation import consolidate
    from ..memory.curated import CuratedStore
    from ..memory.facade import MemoryFacade
    from ..memory.projection import ProjectionWorker
    from ..memory.semantic import SemanticIndex
    from ..memory.state import ConversationStateStore
    from ..memory.summary import TurnSummaryStore
    from ..memory.transcript import TranscriptStore
    from ..memory.worker import MemoryWorker, spawn_worker_process

    settings = _settings_from_args(args)
    data = settings.resolved_data_dir
    if getattr(args, "subprocess", False):
        proc = spawn_worker_process(
            data_dir=data,
            once=not args.loop,
            interval=args.interval,
            stop_when_idle=args.stop_when_idle,
            max_iterations=args.max_iterations,
        )
        out, err = proc.communicate()
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        if err:
            print(err, end="" if err.endswith("\n") else "\n", file=__import__("sys").stderr)
        return int(proc.returncode or 0)
    curated = CuratedStore(path=data / "memory" / "curated.json")
    if getattr(args, "consolidate", False):
        texts = list(getattr(args, "text", None) or [])
        # Also scan recent transcript lines if present
        tpath = data / "sessions" / f"{settings.session_id}.jsonl"
        if tpath.is_file() and not texts:
            for line in tpath.read_text(encoding="utf-8").splitlines()[-50:]:
                try:
                    import json as _json

                    row = _json.loads(line)
                    if row.get("role") == "user" and row.get("content"):
                        texts.append(str(row["content"]))
                except Exception:
                    continue
        report = consolidate(
            curated,
            session_id=settings.session_id,
            texts=texts or None,
            include_session_curated=True,
            apply=bool(getattr(args, "apply", False)),
        )
        print(
            "memory-worker consolidate: "
            f"apply={report['apply']} proposed={report['proposed_count']} "
            f"applied={report['applied_count']} skipped={len(report['skipped'])}"
        )
        for c in report.get("candidates") or []:
            print(f"  - ({c.get('confidence')}) {c.get('content')}")
        for s in report.get("skipped") or []:
            print(f"  skip: {s.get('reason')}: {s.get('content')}")
        return 0
    state = ConversationStateStore(path=data / "memory" / "state.json")
    memory = MemoryFacade(
        transcript=TranscriptStore(path=data / "sessions" / f"{settings.session_id}.jsonl"),
        curated=curated,
        state=state,
        summaries=TurnSummaryStore(path=data / "memory" / "summaries.json"),
        semantic=SemanticIndex(path=data / "memory" / "semantic.json"),
        projection=ProjectionWorker(
            path=data / "memory" / "projection_jobs.json", state_store=state
        ),
    )
    worker = MemoryWorker(memory=memory)
    if args.loop:
        n = await worker.run_loop(
            interval_seconds=args.interval,
            max_iterations=args.max_iterations,
            stop_when_idle=args.stop_when_idle,
        )
        print(f"memory-worker: iterations={n}")
        return 0
    result = await worker.run_once()
    print(
        "memory-worker: "
        f"summaries={result['summaries_processed']} "
        f"projection={result['projection_count']} "
        f"pending_summaries={result['pending_summaries']} "
        f"pending_projection={result['pending_projection']}"
    )
    return 0


async def cmd_toolbox(args: argparse.Namespace) -> int:
    for profile in list_profiles():
        d = profile_as_dict(profile)
        print(f"{d['name']}\t{d['docker_image']}\t{d['description']}")
        if d.get("packages_hint"):
            print(f"  packages: {', '.join(d['packages_hint'])}")
        if d.get("notes"):
            print(f"  notes: {d['notes']}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    parse_argv, free_prompt = extract_free_prompt(raw)
    args = parser.parse_args(parse_argv)

    try:
        if args.command is None:
            # Bare entry / free prompt → interactive (TTY) or safe non-TTY path
            if not sys.stdin.isatty():
                if free_prompt:
                    args.prompt = [free_prompt]
                    code = asyncio.run(cmd_run(args))
                else:
                    print(
                        "ariadne: interactive mode requires a TTY.\n"
                        "  ariadne                  # interactive REPL\n"
                        "  ariadne \"prompt\"         # REPL seeded with prompt\n"
                        "  ariadne run \"prompt\"     # one-shot\n"
                        "  ariadne --help",
                        file=sys.stderr,
                    )
                    code = 2
            else:
                code = cmd_interactive(args, initial_prompt=free_prompt)
        elif args.command in ("run", "exec"):
            code = asyncio.run(cmd_run(args))
        elif args.command == "chat":
            code = cmd_chat(args)
        elif args.command == "resume":
            code = cmd_resume(args)
        elif args.command == "doctor":
            code = asyncio.run(cmd_doctor(args))
        elif args.command == "tools":
            code = asyncio.run(cmd_tools(args))
        elif args.command == "skills":
            code = asyncio.run(cmd_skills(args))
        elif args.command == "memory-worker":
            code = asyncio.run(cmd_memory_worker(args))
        elif args.command == "serve":
            code = cmd_serve(args)
        elif args.command == "sessions":
            code = cmd_sessions(args)
        elif args.command == "plugins":
            code = cmd_plugins(args)
        elif args.command == "plugin":
            code = cmd_plugin(args)
        elif args.command == "toolbox":
            code = asyncio.run(cmd_toolbox(args))
        elif args.command == "atelier":
            from ..atelier.cli import cmd_atelier

            code = cmd_atelier(args)
        elif args.command == "version":
            print(__version__)
            code = 0
        else:
            parser.error(f"unknown command {args.command}")
            code = 2
    except AriadneError as exc:
        print(f"ERROR {exc.error.code}: {exc.error.message}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
