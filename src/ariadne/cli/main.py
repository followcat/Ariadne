from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import __version__
from ..config import load_settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from ..sandbox.toolbox import list_profiles, profile_as_dict
from ..types import TurnEvent
from .render import render_event, render_human, render_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description="Ariadne — personal shell agent (CLI host over the agent kernel).",
    )
    parser.add_argument("--version", action="version", version=f"ariadne {__version__}")
    parser.add_argument("--session", default=None, help="Session id (default: default)")
    parser.add_argument("--workspace", type=Path, default=None, help="Project workspace (default: cwd)")
    parser.add_argument(
        "--sandbox",
        choices=["local", "null", "docker"],
        default=None,
        help="Sandbox backend",
    )
    parser.add_argument(
        "--sandbox-lifecycle",
        choices=["per_turn", "active_session"],
        default=None,
        help="Sandbox lifecycle (chat prefers active_session)",
    )
    parser.add_argument("--toolbox", default=None, help="Toolbox profile: minimal|docs|data")
    parser.add_argument("--docker-image", default=None, help="Override docker image")
    parser.add_argument("--model", default=None, help="Override MODEL from .env")
    parser.add_argument("--tool-loop-limit", type=int, default=None)
    parser.add_argument("--skills-dir", type=Path, default=None, help="Extra skills directory")
    parser.add_argument("--eager-tools", action="store_true", help="Send all tool schemas eagerly")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show tool traces and usage")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Print TurnResult JSON")
    parser.add_argument("--stream", action="store_true", help="Stream model tokens / turn events")
    parser.add_argument(
        "--sandbox-prestart",
        action="store_true",
        help="Start sandbox session in parallel with memory context build",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one agent turn")
    run_p.add_argument("prompt", nargs="+", help="User prompt")

    chat_p = sub.add_parser("chat", help="Interactive multi-turn shell agent")
    chat_p.add_argument("--no-welcome", action="store_true", help="Suppress welcome banner")

    sub.add_parser("doctor", help="Check configuration")
    sub.add_parser("tools", help="List tools")
    sub.add_parser("skills", help="List installed skills")
    sub.add_parser("toolbox", help="List toolbox profiles")
    sub.add_parser("version", help="Print version")
    return parser


def _settings_from_args(args: argparse.Namespace, *, default_lifecycle: str | None = None):
    lifecycle = args.sandbox_lifecycle
    if lifecycle is None and default_lifecycle is not None:
        lifecycle = default_lifecycle
    return load_settings(
        workspace=args.workspace,
        session_id=args.session,
        model=args.model,
        sandbox=args.sandbox,
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
    )


async def _emit_stream(agent, prompt: str, *, json_mode: bool, verbose: bool) -> int:
    final = None
    async for event in agent.run_stream(prompt):
        if event.kind in {"turn_completed", "turn_failed"}:
            final = event.data.get("result")
            continue
        if not json_mode:
            chunk = render_event(event, verbose=verbose)
            if chunk:
                sys.stdout.write(chunk)
                sys.stdout.flush()
    if final is None:
        print("ERROR: stream ended without result", file=sys.stderr)
        return 1
    if json_mode:
        sys.stdout.write(render_json(final))
    else:
        # final human summary (assistant text already streamed if model_delta)
        if not getattr(final, "text", None) or verbose:
            sys.stdout.write(render_human(final, verbose=verbose, skip_text=bool(final.text)))
        elif final.status != "completed":
            sys.stdout.write(render_human(final, verbose=True))
        else:
            # ensure newline after streamed text
            if final.text and not final.text.endswith("\n"):
                sys.stdout.write("\n")
            if verbose:
                sys.stdout.write(render_human(final, verbose=True, skip_text=True))
    return 0 if final.status == "completed" else 1


async def cmd_run(args: argparse.Namespace) -> int:
    settings = _settings_from_args(args)
    agent = compose_agent(settings)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Prompt is empty.", file=sys.stderr)
        return 2
    if settings.stream:
        return await _emit_stream(
            agent, prompt, json_mode=settings.json_mode, verbose=settings.verbose or True
        )
    result = await agent.run(prompt)
    if settings.json_mode:
        sys.stdout.write(render_json(result))
    else:
        sys.stdout.write(render_human(result, verbose=settings.verbose or True))
    return 0 if result.status == "completed" else 1


async def cmd_chat(args: argparse.Namespace) -> int:
    # chat defaults to active_session lifecycle when not specified
    settings = _settings_from_args(args, default_lifecycle="active_session")
    agent = compose_agent(settings)
    if not args.no_welcome:
        print(
            f"Ariadne chat  session={settings.session_id}  workspace={settings.workspace}  "
            f"sandbox={settings.sandbox}/{settings.sandbox_lifecycle}"
        )
        print("Type /exit to quit, /help for commands.")
    while True:
        try:
            line = input("ariadne> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            if agent.active_sessions is not None:
                await agent.active_sessions.close(settings.session_id, reason="chat_exit")
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            if agent.active_sessions is not None:
                await agent.active_sessions.close(settings.session_id, reason="chat_exit")
            return 0
        if line == "/help":
            print(
                "/exit /quit /session /workspace /tools /skills /sandbox-status "
                "/clear-session-files /help"
            )
            continue
        if line == "/session":
            print(settings.session_id)
            continue
        if line == "/workspace":
            print(settings.workspace)
            continue
        if line == "/tools":
            for name in agent.turn_app.tools.tools:
                print(name)
            continue
        if line == "/skills":
            for skill in agent.turn_app.skills.list():
                print(f"{skill.name}\t{skill.description}")
            continue
        if line == "/sandbox-status":
            if agent.active_sessions is None:
                print("lifecycle=per_turn (no active session manager)")
            else:
                print(agent.active_sessions.status())
            continue
        if line == "/clear-session-files":
            if agent.active_sessions is None:
                print("no active sandbox session")
                continue
            ok = await agent.active_sessions.clear_session_files(settings.session_id)
            print("cleared" if ok else "no active session or clear failed")
            continue
        if settings.stream:
            code = await _emit_stream(
                agent, line, json_mode=settings.json_mode, verbose=settings.verbose or True
            )
            if code != 0:
                print(f"(turn failed)", file=sys.stderr)
            continue
        result = await agent.run(line)
        if settings.json_mode:
            sys.stdout.write(render_json(result))
        else:
            sys.stdout.write(render_human(result, verbose=settings.verbose or True))
            if result.status != "completed":
                print(f"(turn status={result.status})", file=sys.stderr)


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

    store = SkillStore.from_dirs(skill_dirs, strict=False, user_root=user_skills)
    for skill in store.list():
        print(f"{skill.name}\t{skill.namespace}\t{skill.description}")
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
            code = asyncio.run(cmd_chat(args))
        elif args.command == "doctor":
            code = asyncio.run(cmd_doctor(args))
        elif args.command == "tools":
            code = asyncio.run(cmd_tools(args))
        elif args.command == "skills":
            code = asyncio.run(cmd_skills(args))
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
