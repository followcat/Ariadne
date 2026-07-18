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
        description="Ariadne — personal shell agent (CLI host over the agent kernel).",
    )
    parser.add_argument("--version", action="version", version=f"ariadne {__version__}")
    _add_global_flags(parser, suppress=False)

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one agent turn")
    _add_global_flags(run_p, suppress=True)
    run_p.add_argument("prompt", nargs="+", help="User prompt")

    chat_p = sub.add_parser("chat", help="Interactive multi-turn shell agent")
    _add_global_flags(chat_p, suppress=True)
    chat_p.add_argument("--no-welcome", action="store_true", help="Suppress welcome banner")
    chat_p.add_argument("--no-stream", action="store_true", help="Disable streaming in chat")

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
    sub.add_parser("toolbox", help="List toolbox profiles")
    sub.add_parser("version", help="Print version")
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
    )


def _apply_continue(args: argparse.Namespace, settings) -> None:
    if getattr(args, "continue_last", False):
        from .sessions import most_recent

        recent = most_recent(settings.resolved_data_dir)
        if recent:
            settings.session_id = recent


def _compose_with_approval(settings):
    from .approval import make_approval_hook

    agent = compose_agent(settings)
    agent.turn_app.approval_hook = make_approval_hook(settings.approval_mode)
    return agent


def _event_to_jsonable(event: TurnEvent) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in event.data.items():
        if key == "result":
            continue
        data[key] = value
    return {"kind": event.kind, "data": data}


async def _emit_stream(agent, prompt: str, *, json_mode: bool, verbose: bool) -> tuple[int, object]:
    final = None
    saw_delta = False
    async for event in agent.run_stream(prompt):
        if event.kind in {"turn_completed", "turn_failed"}:
            final = event.data.get("result")
            continue
        if json_mode:
            # NDJSON event stream (cli-shell-agent §6.2), final result after
            sys.stdout.write(json.dumps(_event_to_jsonable(event), ensure_ascii=False, default=str) + "\n")
            sys.stdout.flush()
            continue
        if event.kind == "model_delta":
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


def cmd_chat(args: argparse.Namespace) -> int:
    # chat defaults to active_session lifecycle when not specified
    settings = _settings_from_args(args, default_lifecycle="active_session")
    _apply_continue(args, settings)
    agent = _compose_with_approval(settings)
    from .repl import run_repl

    return run_repl(args, settings, agent, welcome=not args.no_welcome)


async def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    print(f"workspace:  {settings.workspace}")
    print(f"session:    {settings.session_id}")
    print(f"sandbox:    {settings.sandbox}")
    print(f"lifecycle:  {settings.sandbox_lifecycle}")
    print(f"toolbox:    {settings.toolbox_profile}")
    print(f"model:      {settings.model}")
    print(f"base_url:   {'set' if settings.base_url else 'MISSING'}")
    print(f"api_key:    {'set' if settings.api_key else 'MISSING'}")
    print(f"data_dir:   {settings.resolved_data_dir}")
    print(f"deferred:   {settings.prefer_deferred_tools}")
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
    repo_root = Path(__file__).resolve().parents[3]
    user_skills = settings.resolved_data_dir / "skills" / "user"
    for candidate in (
        repo_root / "skills" / "builtin",
        settings.workspace / "skills",
        settings.skills_dir,
        user_skills,
    ):
        if candidate is not None and Path(candidate).is_dir():
            skill_dirs.append(Path(candidate))
    from ..skills.store import SkillStore

    if args.action == "validate":
        store = SkillStore.from_dirs(skill_dirs, strict=True, user_root=user_skills)
        for skill in store.list():
            print(f"ok\t{skill.name}\t{skill.namespace}")
        print(f"valid: {len(store.list())} skill(s)")
        return 0

    store = SkillStore.from_dirs(skill_dirs, strict=False, user_root=user_skills)
    for skill in store.list():
        print(f"{skill.name}\t{skill.namespace}\t{skill.description}")
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    from ..plugins import PLUGIN_REGISTRY, PluginStore

    settings = _settings_from_args(args)
    store = PluginStore(settings.resolved_data_dir / "plugins.json")
    configured = store.list()
    rows = []
    for name, plugin in sorted(PLUGIN_REGISTRY.items()):
        entry = configured.get(name) or {}
        status = "enabled" if entry.get("enabled") else ("disabled" if entry else "not configured")
        rows.append([name, status, plugin.description])
    ui.print_table(["plugin", "status", "description"], rows)
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    from ..plugins import PLUGIN_REGISTRY, PluginStore

    settings = _settings_from_args(args)
    store = PluginStore(settings.resolved_data_dir / "plugins.json")
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
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            code = asyncio.run(cmd_run(args))
        elif args.command == "chat":
            code = cmd_chat(args)
        elif args.command == "doctor":
            code = asyncio.run(cmd_doctor(args))
        elif args.command == "tools":
            code = asyncio.run(cmd_tools(args))
        elif args.command == "skills":
            code = asyncio.run(cmd_skills(args))
        elif args.command == "sessions":
            code = cmd_sessions(args)
        elif args.command == "plugins":
            code = cmd_plugins(args)
        elif args.command == "plugin":
            code = cmd_plugin(args)
        elif args.command == "toolbox":
            code = asyncio.run(cmd_toolbox(args))
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
