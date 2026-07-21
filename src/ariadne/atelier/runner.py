"""Atelier session: inject user-owned KNOWLEDGE.md into system context.

Auto-extract after turns is **off by default** (Codex AGENTS.md model).
Optional extract helpers remain on knowledge.py for opt-in / tests only.
"""

from __future__ import annotations

from typing import Any

from .knowledge import knowledge_for_inject, read_knowledge
from .models import Project, SessionMeta, SessionType, append_transcript


DEFAULT_ATELIER_POLICY = """You are working inside an Ariadne Atelier (project workshop).
KNOWLEDGE.md is the user's project brief (like Codex AGENTS.md) — treat it as durable policy.
Do not invent project decisions; when the user states a lasting convention, remind them they can edit KNOWLEDGE.md.
Turn-level memory is handled by the Memory system, not by rewriting KNOWLEDGE.md automatically.
"""


def build_system_prompt(project: Project, session: SessionMeta, base: str = "") -> str:
    knowledge = knowledge_for_inject(project)
    parts = [
        base.strip() or DEFAULT_ATELIER_POLICY,
        "",
        "---",
        f"# 当前项目: {project.name}",
        "",
        "## 项目说明 (KNOWLEDGE.md · 用户维护)",
        knowledge,
        "",
    ]
    if session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 分支信息",
                f"你正在实验分支 `{session.branch_name or session.title}` 中。",
                "对话上下文与主会话隔离；代码 workspace 与主会话共享。",
                "实验完成后由用户 merge 或 discard。",
                "",
            ]
        )
    parts.extend(
        [
            "## 工作目录",
            f"Host workspace: {project.workspace_path}",
            "Sandbox path: /workspace",
            "",
        ]
    )
    return "\n".join(parts)


async def update_knowledge_after_turn(
    project: Project,
    session: SessionMeta,
    *,
    user_text: str,
    assistant_text: str,
    settings: Any | None = None,
    use_llm: bool = False,
    enabled: bool = False,
) -> dict[str, Any]:
    """Opt-in post-turn extract. Default enabled=False (no auto write).

    Automatic sedimentation belongs to Memory; KNOWLEDGE is user-led.
    """
    if not enabled:
        return {"updated": False, "reason": "disabled", "source": "none"}
    if session.type != SessionType.MAIN:
        return {"updated": False, "reason": "branch_skip"}

    from .knowledge import (
        apply_updates,
        extract_knowledge_heuristic,
        extract_knowledge_llm,
        make_llm_complete,
        write_knowledge,
    )

    conv = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ]
    complete = make_llm_complete(settings) if use_llm and settings is not None else None
    if complete is not None:
        update = await extract_knowledge_llm(
            conv, read_knowledge(project), complete=complete
        )
        source = "llm"
    else:
        update = extract_knowledge_heuristic(conv)
        source = "heuristic"
    if not update.has_update:
        return {"updated": False, "reason": "no_update", "source": source}
    current = read_knowledge(project)
    new_content = apply_updates(current, update)
    if new_content == current:
        return {"updated": False, "reason": "noop", "source": source}
    write_knowledge(project, new_content, session_id=session.id)
    return {
        "updated": True,
        "source": source,
        "ops": [
            {
                "type": u.type,
                "section": u.section,
                "new_text": u.new_text,
                "old_text": u.old_text,
            }
            for u in update.updates
        ],
    }


def maybe_update_knowledge_after_turn(
    project: Project,
    session: SessionMeta,
    *,
    user_text: str,
    assistant_text: str,
    enabled: bool = False,
) -> bool:
    """Legacy sync helper. Default no-op unless enabled=True (tests opt-in)."""
    if not enabled:
        return False
    if session.type != SessionType.MAIN:
        return False
    from .knowledge import apply_updates, extract_knowledge_heuristic, write_knowledge

    update = extract_knowledge_heuristic(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    )
    if not update.has_update:
        return False
    current = read_knowledge(project)
    new_content = apply_updates(current, update)
    if new_content == current:
        return False
    write_knowledge(project, new_content, session_id=session.id)
    return True


def settings_for_atelier(project: Project, session: SessionMeta, base_settings: Any) -> Any:
    """Bind workspace + session + KNOWLEDGE inject."""
    import dataclasses

    extra = build_system_prompt(project, session)
    return dataclasses.replace(
        base_settings,
        workspace=project.workspace_path,
        session_id=f"atelier-{project.id}-{session.id}",
        data_dir=project.data_dir,
        tool_loop_limit=project.config.max_tool_loop,
        sandbox_profile=project.config.sandbox_profile,
        docker_image=project.config.docker_image,
        sandbox_network=project.config.network_mode,
        extra_system_prompt=extra,
    )


def notify_knowledge_updated(project: Project, session_id: str) -> None:
    append_transcript(
        project,
        session_id,
        {
            "role": "system",
            "content": "[atelier] KNOWLEDGE.md was edited.",
            "session_id": session_id,
        },
    )
