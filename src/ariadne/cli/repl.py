"""Interactive REPL for `ariadne chat`.

Codex/Claude-Code style terminal agent loop:
- readline history persisted under the data dir
- multiline input (trailing \\ continuation, ``` blocks)
- Ctrl+C cancels the current turn, not the REPL
- streaming by default (--no-stream to disable)
- spinner while the model thinks, live deltas when streaming
- prompt shows session:model
"""

from __future__ import annotations

import argparse
import asyncio
import readline
import sys
import uuid
from pathlib import Path
from typing import Any

from ..agent import Agent
from ..config import Settings
from ..errors import AriadneError
from ..host.compose import compose_agent
from . import ui
from .render import render_json

HISTORY_LIMIT = 5000


def _setup_readline(history_path: Path) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(str(history_path))
    except FileNotFoundError:
        pass
    readline.set_history_length(HISTORY_LIMIT)


def _save_readline(history_path: Path) -> None:
    readline.write_history_file(str(history_path))


def _read_multiline(prompt: str) -> str | None:
    """input() with \\ continuation and ``` blocks. None on EOF/Ctrl+C."""
    try:
        first = input(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    lines = [first]
    # ``` fence toggles block mode
    if first.strip().startswith("```") and first.strip().count("```") == 1:
        while True:
            try:
                line = input("... ")
            except (EOFError, KeyboardInterrupt):
                return None
            lines.append(line)
            if line.strip() == "```":
                break
        return "\n".join(lines)
    while lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1]
        try:
            lines.append(input("... "))
        except (EOFError, KeyboardInterrupt):
            return None
    return "\n".join(lines)


async def _run_turn(
    agent: Agent, line: str, *, stream: bool, json_mode: bool, verbose: bool
) -> tuple[int, Any]:
    from ..cli.main import _emit_stream  # shared stream renderer

    if stream:
        return await _emit_stream(agent, line, json_mode=json_mode, verbose=verbose)
    spinner = ui.status("thinking…")
    spinner.__enter__()
    try:
        result = await agent.run(line)
    finally:
        spinner.__exit__(None, None, None)
    if json_mode:
        sys.stdout.write(render_json(result))
    else:
        ui.render_result(result, verbose=verbose)
        if result.status != "completed":
            ui.print_info(f"(turn status={result.status})")
    return (0 if result.status == "completed" else 1), result


def run_repl(
    args: argparse.Namespace,
    settings: Settings,
    agent: Agent,
    *,
    welcome: bool = True,
) -> int:
    history_path = settings.resolved_data_dir / "history"
    _setup_readline(history_path)
    stream = settings.stream or not getattr(args, "no_stream", False)
    usage_totals = {"prompt": 0, "completion": 0, "total": 0, "turns": 0}

    if welcome:
        ui.print_info(
            f"Ariadne chat  session={settings.session_id}  workspace={settings.workspace}  "
            f"sandbox={settings.sandbox}/{settings.sandbox_lifecycle}  approval={settings.approval_mode}"
        )
        ui.print_info("Type /exit to quit, /help for commands.")

    exit_code = 0
    try:
        while True:
            prompt = f"{settings.session_id}:{settings.model}> "
            line = _read_multiline(prompt)
            if line is None:
                ui.out.print()
                break
            line = line.strip()
            if not line:
                continue

            if line in {"/exit", "/quit"}:
                break
            if line == "/help":
                ui.out.print(
                    "/exit /quit /session /workspace /tools /skills /model [name] "
                    "/memory read /usage /compact /resume [id] /reset-session "
                    "/sandbox-status /clear-session-files /clear /help",
                    markup=False,
                )
                continue
            if line == "/session":
                ui.out.print(settings.session_id)
                continue
            if line == "/workspace":
                ui.out.print(str(settings.workspace))
                continue
            if line == "/tools":
                for name in agent.turn_app.tools.tools:
                    ui.out.print(name)
                continue
            if line == "/skills":
                for skill in agent.turn_app.skills.list():
                    ui.out.print(f"{skill.name}\t{skill.description}")
                continue
            if line.startswith("/model"):
                parts = line.split(maxsplit=1)
                if len(parts) == 1:
                    ui.out.print(settings.model)
                else:
                    settings.model = parts[1].strip()
                    agent.model = settings.model
                    ui.print_info(f"model -> {settings.model}")
                continue
            if line.startswith("/memory"):
                parts = line.split()
                action = parts[1] if len(parts) > 1 else "read"
                if action != "read":
                    ui.out.print("usage: /memory read")
                    continue
                curated = asyncio.run(agent.get_curated(session_id=settings.session_id))
                for scope in ("user", "session"):
                    entries = curated.get(scope) or []
                    ui.out.print(f"[{scope}] {len(entries)} entries")
                    for entry in entries:
                        ui.out.print(f"  {entry['id']}. {entry['content']}")
                continue
            if line == "/usage":
                ui.out.print(
                    f"turns={usage_totals['turns']} prompt={usage_totals['prompt']} "
                    f"completion={usage_totals['completion']} total={usage_totals['total']}"
                )
                continue
            if line == "/compact":
                transcript = agent.turn_app.memory.transcript
                if transcript.path.exists() and transcript.path.stat().st_size:
                    backup = transcript.path.with_suffix(".jsonl.bak")
                    backup.write_text(transcript.path.read_text(encoding="utf-8"), encoding="utf-8")
                    transcript.path.write_text("", encoding="utf-8")
                    ui.print_info(f"transcript archived to {backup.name}; summaries still cover history")
                else:
                    ui.print_info("transcript already empty")
                continue
            if line == "/clear":
                ui.out.clear()
                continue
            if line == "/reset-session":
                if agent.active_sessions is not None:
                    asyncio.run(agent.active_sessions.close(settings.session_id, reason="reset_session"))
                settings.session_id = f"reset-{uuid.uuid4().hex[:8]}"
                from .approval import make_approval_hook

                agent = compose_agent(settings)
                agent.turn_app.approval_hook = make_approval_hook(settings.approval_mode)
                ui.print_info(f"new session: {settings.session_id} (workspace kept)")
                continue
            if line == "/sandbox-status":
                if agent.active_sessions is None:
                    ui.out.print("lifecycle=per_turn (no active session manager)")
                else:
                    ui.out.print(str(agent.active_sessions.status()))
                continue
            if line == "/clear-session-files":
                if agent.active_sessions is None:
                    ui.out.print("no active sandbox session")
                    continue
                ok = asyncio.run(agent.active_sessions.clear_session_files(settings.session_id))
                ui.out.print("cleared" if ok else "no active session or clear failed")
                continue

            try:
                code, result = asyncio.run(
                    _run_turn(
                        agent,
                        line,
                        stream=stream,
                        json_mode=settings.json_mode,
                        verbose=settings.verbose,
                    )
                )
            except KeyboardInterrupt:
                ui.print_info("^C (turn cancelled; sandbox cleaned up)")
                continue
            except AriadneError as exc:
                ui.print_error(exc.error.code, exc.error.message)
                exit_code = 1
                continue
            usage_totals["turns"] += 1
            if result is not None:
                usage_totals["prompt"] += result.usage.prompt_tokens
                usage_totals["completion"] += result.usage.completion_tokens
                usage_totals["total"] += result.usage.total_tokens
            if code != 0:
                exit_code = 1
    finally:
        if agent.active_sessions is not None:
            asyncio.run(agent.active_sessions.close(settings.session_id, reason="chat_exit"))
        _save_readline(history_path)
    return exit_code
