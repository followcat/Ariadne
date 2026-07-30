"""Interactive REPL for bare `ariadne` / `ariadne chat`.

Codex-style terminal agent loop:
- readline history persisted under the data dir
- multiline input (trailing \\ continuation, ``` blocks)
- Ctrl+C cancels the current turn, not the REPL
- streaming by default (--no-stream to disable)
- spinner while the model thinks, live deltas when streaming
- optional initial_prompt seeds the first turn
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

_HELP_TEXT = """\
session
  /session              show session id
  /resume [id]          list or switch sessions
  /new | /reset-session new session id (workspace kept)
  /status               host status (model, workspace, approval, vision, …)
  /mode [auto|on-request|readonly]
  /model [name]         show or hot-swap model
  /usage                cumulative tokens this REPL
  /compact              archive transcript (summaries remain)

images
  /image [path]         attach from path, or clipboard if omitted
  /images               list pending attachments
  /clear-images         drop pending images
  Non-vision models refuse with ARIADNE_MULTIMODAL_UNSUPPORTED
  (override with ARIADNE_VISION=on).

title (session topic summary)
  /title                show current session title
  /title <text>         set user title (won’t auto-overwrite)
  /title --refresh      re-summarize topic from recent messages

workspace / tools
  /workspace            show workspace path
  /tools                list tools
  /skills               list skills
  /sandbox-status
  /clear-session-files  wipe /session scratch (not /workspace)
  /memory read

ui
  /clear                clear screen
  /help
  /exit | /quit

input: trailing \\ continues a line; ``` fences open a multiline block.
Ctrl+C cancels the running turn; Ctrl+C on an empty prompt exits.
"""


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
    agent: Agent,
    line: str,
    *,
    stream: bool,
    json_mode: bool,
    verbose: bool,
    images: list | None = None,
    task_mode: bool = False,
) -> tuple[int, Any]:
    from ..cli.main import _emit_stream  # shared stream renderer

    if stream:
        return await _emit_stream(
            agent,
            line,
            json_mode=json_mode,
            verbose=verbose,
            images=images,
            metadata={"task_mode": True} if task_mode else None,
        )
    spinner = ui.status("thinking…")
    spinner.__enter__()
    try:
        result = await agent.run(
            line,
            images=images,
            metadata={"task_mode": True} if task_mode else None,
        )
    finally:
        spinner.__exit__(None, None, None)
    if json_mode:
        sys.stdout.write(render_json(result))
    else:
        ui.render_result(result, verbose=verbose)
        if result.status != "completed":
            ui.print_info(f"(turn status={result.status})")
    return (0 if result.status == "completed" else 1), result


def _print_status(settings: Settings, agent: Agent) -> None:
    from ..multimodal import model_supports_vision

    ui.out.print(f"session:    {settings.session_id}")
    ui.out.print(f"workspace:  {settings.workspace}")
    ui.out.print(f"model:      {settings.model}")
    ui.out.print(f"base_url:   {'set' if settings.base_url else 'MISSING'}")
    ui.out.print(f"api_key:    {'set' if settings.api_key else 'MISSING'}")
    ui.out.print(f"sandbox:    {settings.sandbox}/{settings.sandbox_lifecycle}")
    ui.out.print(f"approval:   {settings.approval_mode}")
    ui.out.print(f"stream:     {settings.stream}")
    vision = getattr(settings, "vision", "auto")
    supports = model_supports_vision(settings.model, vision_mode=vision)
    ui.out.print(f"vision:     {vision} (model multimodal: {'yes' if supports else 'no'})")
    if agent.active_sessions is not None:
        ui.out.print(f"active:     {agent.active_sessions.status()}")


def _attach_approval(settings: Settings, agent: Agent) -> None:
    from .approval import make_approval_hook
    from .grants import GrantStore

    agent.turn_app.approval_hook = make_approval_hook(
        settings.approval_mode,
        grant_store=GrantStore(path=settings.resolved_data_dir / "grants.json"),
        session_id=settings.session_id,
    )


def _reset_session(settings: Settings, agent: Agent) -> Agent:
    if agent.active_sessions is not None:
        asyncio.run(agent.active_sessions.close(settings.session_id, reason="reset_session"))
    settings.session_id = f"reset-{uuid.uuid4().hex[:8]}"
    agent = compose_agent(settings)
    _attach_approval(settings, agent)
    return agent


def run_repl(
    args: argparse.Namespace,
    settings: Settings,
    agent: Agent,
    *,
    welcome: bool = True,
    initial_prompt: str | None = None,
) -> int:
    history_path = settings.resolved_data_dir / "history"
    _setup_readline(history_path)
    stream = settings.stream or not getattr(args, "no_stream", False)
    # keep settings.stream aligned for /status
    settings.stream = stream
    usage_totals = {"prompt": 0, "completion": 0, "total": 0, "turns": 0}
    pending_images: list[Any] = []

    if welcome:
        ws = str(settings.workspace)
        if len(ws) > 48:
            ws = "…" + ws[-47:]
        ui.print_info(
            f"Ariadne  session={settings.session_id}  model={settings.model}  "
            f"workspace={ws}  sandbox={settings.sandbox}/{settings.sandbox_lifecycle}  "
            f"approval={settings.approval_mode}"
        )
        ui.print_info("Type /help for commands, /image to attach, /exit to quit.")

    exit_code = 0

    def _consume_turn(line: str) -> None:
        nonlocal exit_code, agent, pending_images
        imgs = list(pending_images)
        try:
            code, result = asyncio.run(
                _run_turn(
                    agent,
                    line,
                    stream=stream,
                    json_mode=settings.json_mode,
                    verbose=settings.verbose,
                    images=imgs or None,
                    task_mode=bool(getattr(args, "task", False)),
                )
            )
        except KeyboardInterrupt:
            ui.print_info("^C (turn cancelled; sandbox cleaned up)")
            return
        except AriadneError as exc:
            ui.print_error(exc.error.code, exc.error.message)
            if exc.error.code == "ARIADNE_MULTIMODAL_UNSUPPORTED":
                ui.print_info(
                    "提示: 当前模型不支持图片。可 /clear-images 后只发文字，"
                    "或换 vision 模型 / 设置 ARIADNE_VISION=on。"
                )
            exit_code = 1
            return
        pending_images.clear()
        usage_totals["turns"] += 1
        if result is not None:
            usage_totals["prompt"] += result.usage.prompt_tokens
            usage_totals["completion"] += result.usage.completion_tokens
            usage_totals["total"] += result.usage.total_tokens
            # Auto topic title after successful turns (does not overwrite user titles)
            try:
                from .sessions import refresh_session_title

                meta = refresh_session_title(
                    settings.resolved_data_dir, settings.session_id, force=False
                )
                if meta and not meta.get("skipped") and usage_totals["turns"] <= 2:
                    ui.print_info(f"title -> {meta.get('title')} (auto)")
            except Exception:  # noqa: BLE001 — title is best-effort
                pass
        if code != 0:
            exit_code = 1

    try:
        if initial_prompt and initial_prompt.strip():
            ui.print_info(f"→ {initial_prompt.strip()[:120]}")
            _consume_turn(initial_prompt.strip())

        while True:
            img_mark = f"[{len(pending_images)} img] " if pending_images else ""
            prompt = f"{img_mark}{settings.session_id}:{settings.model}> "
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
                ui.out.print(_HELP_TEXT, markup=False)
                continue
            if line == "/status":
                _print_status(settings, agent)
                from .sessions import get_session_title

                t, src = get_session_title(settings.resolved_data_dir, settings.session_id)
                ui.out.print(f"title:      {t or '(none)'} ({src or 'unset'})")
                if pending_images:
                    ui.out.print(f"pending_images: {len(pending_images)}")
                    for i, img in enumerate(pending_images, 1):
                        ui.out.print(f"  {i}. {img.name} ({img.mime}, {len(img.data)} bytes)")
                continue
            if line.startswith("/title"):
                from .sessions import get_session_title, refresh_session_title, set_session_title

                parts = line.split(maxsplit=1)
                data_dir = settings.resolved_data_dir
                if len(parts) == 1:
                    t, src = get_session_title(data_dir, settings.session_id)
                    ui.out.print(t or "(no title yet — send a turn or /title --refresh)")
                    if src:
                        ui.print_info(f"source={src}")
                    continue
                arg = parts[1].strip()
                if arg in {"--refresh", "-r", "refresh"}:
                    meta = refresh_session_title(data_dir, settings.session_id, force=True)
                    if meta is None:
                        ui.print_error("TITLE", "empty session — nothing to summarize")
                    elif meta.get("skipped"):
                        ui.print_info(f"title kept (user): {meta.get('title')}")
                    else:
                        ui.print_info(f"title -> {meta.get('title')} (auto)")
                    continue
                try:
                    meta = set_session_title(data_dir, settings.session_id, arg, source="user")
                except ValueError as exc:
                    ui.print_error("TITLE", str(exc))
                    continue
                ui.print_info(f"title -> {meta['title']} (user)")
                continue
            if line == "/images":
                if not pending_images:
                    ui.print_info("no pending images (use /image [path] or clipboard)")
                else:
                    for i, img in enumerate(pending_images, 1):
                        ui.out.print(f"{i}. {img.name}  {img.mime}  {len(img.data)} bytes")
                continue
            if line == "/clear-images":
                pending_images.clear()
                ui.print_info("pending images cleared")
                continue
            if line.startswith("/image"):
                from ..multimodal import (
                    MAX_IMAGES_PER_TURN,
                    load_image_path,
                    model_supports_vision,
                    read_clipboard_image,
                )

                parts = line.split(maxsplit=1)
                try:
                    if len(parts) > 1 and parts[1].strip():
                        img = load_image_path(parts[1].strip())
                    else:
                        img = read_clipboard_image()
                        if img is None:
                            ui.print_error(
                                "IMAGE",
                                "clipboard has no image — pass a path: /image ./shot.png "
                                "(Linux: copy image then /image, needs xclip or wl-paste)",
                            )
                            continue
                except AriadneError as exc:
                    ui.print_error(exc.error.code, exc.error.message)
                    continue
                if len(pending_images) >= MAX_IMAGES_PER_TURN:
                    ui.print_error("IMAGE", f"max {MAX_IMAGES_PER_TURN} images per turn")
                    continue
                pending_images.append(img)
                supports = model_supports_vision(
                    settings.model, vision_mode=getattr(settings, "vision", "auto")
                )
                ui.print_info(
                    f"attached {img.name} ({img.mime}, {len(img.data)} bytes)  "
                    f"pending={len(pending_images)}"
                    + ("" if supports else "  ⚠ model may not support vision")
                )
                if not supports:
                    ui.print_info(
                        "当前模型未判定为多模态；发送时会提示 ARIADNE_MULTIMODAL_UNSUPPORTED，"
                        "除非设置 ARIADNE_VISION=on。"
                    )
                continue
            if line.startswith("/mode"):
                parts = line.split(maxsplit=1)
                if len(parts) == 1:
                    ui.out.print(settings.approval_mode)
                    continue
                mode = parts[1].strip()
                if mode not in {"auto", "on-request", "readonly"}:
                    ui.print_error("MODE", "usage: /mode [auto|on-request|readonly]")
                    continue
                settings.approval_mode = mode
                _attach_approval(settings, agent)
                ui.print_info(f"approval -> {mode}")
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
            if line.startswith("/resume"):
                from datetime import datetime

                from .sessions import list_sessions

                parts = line.split(maxsplit=1)
                sessions = list_sessions(settings.resolved_data_dir)
                if len(parts) == 1:
                    if not sessions:
                        ui.print_info("no sessions recorded")
                        continue
                    ui.print_table(
                        ["session", "turns", "updated"],
                        [
                            [
                                s.session_id,
                                str(s.turns),
                                datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M"),
                            ]
                            for s in sessions
                        ],
                    )
                    ui.print_info("usage: /resume <session-id>")
                    continue
                target = parts[1].strip()
                if not any(s.session_id == target for s in sessions):
                    ui.print_error("SESSION", f"unknown session: {target}")
                    continue
                if agent.active_sessions is not None:
                    asyncio.run(agent.active_sessions.close(settings.session_id, reason="resume"))
                settings.session_id = target
                agent = compose_agent(settings)
                _attach_approval(settings, agent)
                ui.print_info(f"resumed session: {target}")
                continue
            if line in {"/reset-session", "/new"}:
                agent = _reset_session(settings, agent)
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

            _consume_turn(line)
    finally:
        if agent.active_sessions is not None:
            asyncio.run(agent.active_sessions.close(settings.session_id, reason="chat_exit"))
        _save_readline(history_path)
    return exit_code
