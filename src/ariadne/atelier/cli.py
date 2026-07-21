"""Atelier CLI handlers (registered from cli/main.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..errors import AriadneError
from .knowledge import list_knowledge_history, read_knowledge, write_knowledge, heuristic_refresh
from .manager import AtelierManager
from .models import default_atelier_root
from .runner import build_system_prompt, settings_for_atelier


def _mgr(args: Any) -> AtelierManager:
    root = getattr(args, "atelier_root", None)
    return AtelierManager(Path(root).expanduser() if root else default_atelier_root())


def cmd_atelier(args: Any) -> int:
    action = getattr(args, "atelier_cmd", None)
    if action == "create":
        return _create(args)
    if action == "list":
        return _list(args)
    if action == "open":
        return _open(args)
    if action == "delete":
        return _delete(args)
    if action == "branch":
        return _branch(args)
    if action == "knowledge":
        return _knowledge(args)
    print("usage: ariadne atelier {create|list|open|delete|branch|knowledge}", file=sys.stderr)
    return 2


def _create(args: Any) -> int:
    mgr = _mgr(args)
    from_path = Path(args.from_path).expanduser() if getattr(args, "from_path", None) else None
    try:
        project = mgr.create_project(
            args.name,
            from_path=from_path,
            no_scan=bool(getattr(args, "no_scan", False)),
        )
    except AriadneError as exc:
        print(exc.error.message, file=sys.stderr)
        return 1
    print(f"created atelier {project.id}")
    print(f"  path:      {project.path}")
    print(f"  workspace: {project.workspace_path}")
    print(f"  knowledge: {project.knowledge_path}")
    print(f"open with: ariadne atelier open {project.id}")
    return 0


def _list(args: Any) -> int:
    mgr = _mgr(args)
    projects = mgr.list_projects()
    if not projects:
        print("(no ateliers)")
        return 0
    for p in projects:
        print(f"{p.id}\t{p.name}\t{p.workspace_path}")
    return 0


def _delete(args: Any) -> int:
    mgr = _mgr(args)
    try:
        mgr.delete_project(args.name, yes=bool(getattr(args, "yes", False)))
    except AriadneError as exc:
        print(exc.error.message, file=sys.stderr)
        return 1
    print(f"deleted atelier {args.name}")
    return 0


def _open(args: Any) -> int:
    """Bind workspace/session and enter existing interactive REPL."""
    from ..cli.main import _settings_from_args
    from ..cli.repl import run_repl
    from ..host.compose import compose_agent
    from ..cli.main import _compose_with_approval

    mgr = _mgr(args)
    try:
        project = mgr.get_project(args.name)
        sid = getattr(args, "session", None) or "main"
        if sid == "main":
            session = mgr.get_or_create_main_session(project.id)
        else:
            # allow branch-foo or bare foo
            try:
                session = mgr.get_session(project.id, sid)
            except AriadneError:
                session = mgr.get_session(project.id, f"branch-{sid}")
    except AriadneError as exc:
        print(exc.error.message, file=sys.stderr)
        return 1

    # Force local sandbox when docker unavailable for smoother atelier UX? No — respect settings.
    # Prefer active_session lifecycle for multi-turn atelier.
    base = _settings_from_args(args, default_lifecycle="active_session")
    # Avoid docker hard-require if user wants local for atelier open
    settings = settings_for_atelier(project, session, base)
    # Inject knowledge into a file the REPL can show; turn system uses TurnApplication SYSTEM_POLICY.
    # Prefix: write atelier context into data_dir note for user
    note = project.data_dir / "ATELIER_CONTEXT.md"
    note.write_text(build_system_prompt(project, session), encoding="utf-8")

    try:
        agent = _compose_with_approval(settings)
    except AriadneError as exc:
        # Fallback local if docker missing
        if "docker" in (exc.error.message or "").lower():
            import dataclasses

            settings = dataclasses.replace(settings, sandbox="local")
            agent = _compose_with_approval(settings)
            print("note: docker unavailable — using --sandbox local for this atelier open", file=sys.stderr)
        else:
            print(exc.error.message, file=sys.stderr)
            return 1

    print(f"Atelier: {project.name}  session={session.id} ({session.type.value})")
    print(f"workspace: {project.workspace_path}")
    print(f"knowledge: {project.knowledge_path}  (see also {note})")
    return run_repl(args, settings, agent, welcome=True)


def _branch(args: Any) -> int:
    mgr = _mgr(args)
    sub = getattr(args, "branch_cmd", None)
    try:
        if sub == "create":
            meta = mgr.create_branch(args.project, args.branch_name)
            print(f"created branch session {meta.id}")
            print(f"open: ariadne atelier open {args.project} --session {meta.id}")
            return 0
        if sub == "list":
            for s in mgr.list_sessions(args.project):
                if s.type.value == "branch" or s.id == "main":
                    print(f"{s.id}\t{s.type.value}\t{s.status.value}\t{s.title}")
            return 0
        if sub == "merge":
            summary = mgr.merge_branch(args.project, args.branch_name)
            print("merged.")
            print(summary)
            return 0
        if sub == "discard":
            mgr.discard_branch(args.project, args.branch_name)
            print(f"discarded branch {args.branch_name}")
            return 0
    except AriadneError as exc:
        print(exc.error.message, file=sys.stderr)
        return 1
    print("usage: ariadne atelier branch {create|list|merge|discard}", file=sys.stderr)
    return 2


def _knowledge(args: Any) -> int:
    mgr = _mgr(args)
    try:
        project = mgr.get_project(args.project)
    except AriadneError as exc:
        print(exc.error.message, file=sys.stderr)
        return 1
    sub = getattr(args, "knowledge_cmd", None)
    if sub == "show":
        print(read_knowledge(project))
        return 0
    if sub == "edit":
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        path = project.knowledge_path
        if not path.is_file():
            write_knowledge(project, read_knowledge(project))
        subprocess.call([editor, str(path)])
        return 0
    if sub == "refresh":
        write_knowledge(project, heuristic_refresh(project), session_id="refresh")
        print(f"refreshed {project.knowledge_path}")
        return 0
    if sub == "history":
        hist = list_knowledge_history(project)
        if not hist:
            print("(no history)")
            return 0
        for p in hist:
            print(p.name)
        return 0
    print("usage: ariadne atelier knowledge {show|edit|refresh|history}", file=sys.stderr)
    return 2
