from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .. import __version__
from ..config import load_settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from .render import render_human, render_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description="Ariadne — personal shell agent (CLI host over the agent kernel).",
    )
    parser.add_argument("--version", action="version", version=f"ariadne {__version__}")
    parser.add_argument("--session", default=None, help="Session id (default: default)")
    parser.add_argument("--workspace", type=Path, default=None, help="Project workspace (default: cwd)")
    parser.add_argument("--sandbox", choices=["local", "null"], default=None, help="Sandbox backend")
    parser.add_argument("--model", default=None, help="Override MODEL from .env")
    parser.add_argument("--tool-loop-limit", type=int, default=None)
    parser.add_argument("-v", "--verbose", action="store_true", help="Show tool traces and usage")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Print TurnResult JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run one agent turn")
    run_p.add_argument("prompt", nargs="+", help="User prompt")

    chat_p = sub.add_parser("chat", help="Interactive multi-turn shell agent")
    chat_p.add_argument("--no-welcome", action="store_true", help="Suppress welcome banner")

    sub.add_parser("doctor", help="Check configuration")
    sub.add_parser("tools", help="List tools")
    sub.add_parser("version", help="Print version")
    return parser


async def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(
        workspace=args.workspace,
        session_id=args.session,
        model=args.model,
        sandbox=args.sandbox,
        tool_loop_limit=args.tool_loop_limit,
        verbose=args.verbose,
        json_mode=args.json_mode,
    )
    agent = compose_agent(settings)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        print("Prompt is empty.", file=sys.stderr)
        return 2
    result = await agent.run(prompt)
    if settings.json_mode:
        sys.stdout.write(render_json(result))
    else:
        sys.stdout.write(render_human(result, verbose=settings.verbose or True))
    return 0 if result.status == "completed" else 1


async def cmd_chat(args: argparse.Namespace) -> int:
    settings = load_settings(
        workspace=args.workspace,
        session_id=args.session,
        model=args.model,
        sandbox=args.sandbox,
        tool_loop_limit=args.tool_loop_limit,
        verbose=args.verbose,
        json_mode=args.json_mode,
    )
    agent = compose_agent(settings)
    if not args.no_welcome:
        print(f"Ariadne chat  session={settings.session_id}  workspace={settings.workspace}")
        print("Type /exit to quit, /help for commands.")
    while True:
        try:
            line = input("ariadne> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/help":
            print("/exit /quit /session /workspace /tools /help")
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
        result = await agent.run(line)
        if settings.json_mode:
            sys.stdout.write(render_json(result))
        else:
            sys.stdout.write(render_human(result, verbose=settings.verbose or True))
            if result.status != "completed":
                print(f"(turn status={result.status})", file=sys.stderr)


async def cmd_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(
        workspace=args.workspace,
        session_id=args.session,
        model=args.model,
        sandbox=args.sandbox,
    )
    print(f"workspace: {settings.workspace}")
    print(f"session:   {settings.session_id}")
    print(f"sandbox:   {settings.sandbox}")
    print(f"model:     {settings.model}")
    print(f"base_url:  {'set' if settings.base_url else 'MISSING'}")
    print(f"api_key:   {'set' if settings.api_key else 'MISSING'}")
    print(f"data_dir:  {settings.resolved_data_dir}")
    if not settings.base_url or not settings.api_key:
        print("FAIL: configure BASE_URL and API_KEY in .env")
        return 1
    try:
        agent = compose_agent(settings)
        print(f"tools:     {', '.join(agent.turn_app.tools.tools)}")
        print("OK")
        return 0
    except AriadneError as exc:
        print(f"FAIL: {exc.error.code}: {exc.error.message}")
        return 1


async def cmd_tools(args: argparse.Namespace) -> int:
    settings = load_settings(workspace=args.workspace, sandbox=args.sandbox or "null")
    # tools listing should not require API key hard-fail for null compose — use registry only
    from ..tools.registry import build_default_registry

    reg = build_default_registry()
    for name, spec in reg.tools.items():
        print(f"{name}\t{spec.catalog_description or spec.description[:60]}")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(__version__)
            raise SystemExit(0)
        if args.command == "run":
            code = asyncio.run(cmd_run(args))
        elif args.command == "chat":
            code = asyncio.run(cmd_chat(args))
        elif args.command == "doctor":
            code = asyncio.run(cmd_doctor(args))
        elif args.command == "tools":
            code = asyncio.run(cmd_tools(args))
        else:
            parser.error(f"unknown command {args.command}")
            code = 2
    except AriadneError as exc:
        print(f"ERROR {exc.error.code}: {exc.error.message}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(code)


if __name__ == "__main__":
    main()
