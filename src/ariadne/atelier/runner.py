"""SessionRunner: inject KNOWLEDGE into system context and optional post-turn update."""

from __future__ import annotations

from typing import Any

from .knowledge import (
    extract_knowledge_heuristic,
    read_knowledge,
    write_knowledge,
    apply_updates,
)
from .models import Project, SessionMeta, SessionType, append_transcript
from .manager import AtelierManager


DEFAULT_ATELIER_POLICY = """You are working inside an Ariadne Atelier (project workshop).
Prefer durable decisions and conventions to be recorded for the team knowledge base.
"""


def build_system_prompt(project: Project, session: SessionMeta, base: str = "") -> str:
    knowledge = read_knowledge(project)
    parts = [
        base.strip() or DEFAULT_ATELIER_POLICY,
        "",
        "---",
        f"# 当前项目: {project.name}",
        "",
        "## 项目知识库 (KNOWLEDGE.md)",
        knowledge[:12000],
        "",
    ]
    if session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 分支信息",
                f"你正在实验分支 `{session.branch_name or session.title}` 中。",
                "对话上下文与主会话隔离；代码 workspace 与主会话共享。",
                "实验完成后由用户 merge（沉淀知识）或 discard。",
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


def maybe_update_knowledge_after_turn(
    project: Project,
    session: SessionMeta,
    *,
    user_text: str,
    assistant_text: str,
) -> bool:
    """Heuristic knowledge extract for MAIN sessions only. Returns True if wrote."""
    if session.type != SessionType.MAIN:
        return False
    conv = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ]
    update = extract_knowledge_heuristic(conv)
    if not update.has_update:
        return False
    current = read_knowledge(project)
    new_content = apply_updates(current, update)
    if new_content == current:
        return False
    write_knowledge(project, new_content, session_id=session.id)
    return True


def settings_for_atelier(project: Project, session: SessionMeta, base_settings: Any) -> Any:
    """Return a copy of Settings bound to atelier workspace + session id."""
    import dataclasses

    return dataclasses.replace(
        base_settings,
        workspace=project.workspace_path,
        session_id=f"atelier-{project.id}-{session.id}",
        data_dir=project.data_dir,
        tool_loop_limit=project.config.max_tool_loop,
        sandbox_profile=project.config.sandbox_profile,
        docker_image=project.config.docker_image,
        sandbox_network=project.config.network_mode,
    )


def notify_knowledge_updated(project: Project, session_id: str) -> None:
    append_transcript(
        project,
        session_id,
        {
            "role": "system",
            "content": "[atelier] KNOWLEDGE.md updated from recent dialogue.",
            "session_id": session_id,
        },
    )
