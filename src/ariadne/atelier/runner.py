"""Atelier session: inject user-owned KNOWLEDGE.md + workspace snapshot.

Auto-extract after turns is **off by default** (Codex AGENTS.md model).
Delivery policy steers implement tasks to write files and non-empty replies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .knowledge import knowledge_for_inject, read_knowledge, workspace_tree_lines
from .models import Project, SessionMeta, SessionType, append_transcript


DEFAULT_ATELIER_POLICY = """You are working inside an Ariadne Atelier (project workshop).

## Delivery rules (mandatory)
1. Implement / change / draw-in-code tasks MUST write files under `/workspace` using
   `sandbox_write_file`, `sandbox_edit_file`, or shell redirects. Never end after only
   reading or thinking.
2. Your final message MUST be non-empty visible text (Chinese preferred): what changed,
   which paths, how to verify (e.g. open `index.html` in a browser).
3. "画出来 / 绘制" in a code project means: add runnable code (function, button, demo)
   in the existing stack — not a headless browser screenshot, and not reasoning alone.
4. Prefer editing existing entry files (`index.html`, main scripts) over inventing a
   parallel stack.

## Knowledge
KNOWLEDGE.md is the user's short project brief (like Codex AGENTS.md). Treat it as
durable policy. Do not invent decisions; if the user states a lasting convention,
remind them they can edit KNOWLEDGE.md. Turn-level memory is Memory L0–L4, not
automatic KNOWLEDGE rewrites.
"""


def agent_session_id(project: Project, session: SessionMeta) -> str:
    """Stable agent session id without double ``atelier-`` prefixes."""
    return f"aw-{project.id}-{session.id}"


def _main_session_summary_line(project: Project) -> str | None:
    """Best-effort last L1 summary from main agent session (branch continuity)."""
    path = project.data_dir / "memory" / "summaries.json"
    if not path.is_file():
        return None
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    # Prefer current id scheme; also probe legacy double-atelier prefix.
    candidates = [
        agent_session_id(project, SessionMeta(
            id="main",
            project_id=project.id,
            title="Main",
            type=SessionType.MAIN,
        )),
        f"atelier-{project.id}-main",
        f"atelier-atelier-{project.id}-main" if project.id.startswith("atelier-") else "",
        "main",
    ]
    for sid in candidates:
        if not sid:
            continue
        bucket = data.get(sid)
        if not isinstance(bucket, dict) or not bucket:
            continue
        # latest by updated_at
        best_text = ""
        best_ts = -1.0
        for _tid, row in bucket.items():
            if not isinstance(row, dict):
                continue
            ts = float(row.get("updated_at") or 0)
            text = str(row.get("summary_text") or row.get("source_text") or "").strip()
            if text and ts >= best_ts:
                best_ts = ts
                best_text = text
        if best_text:
            return best_text[:400]
    return None


def build_system_prompt(project: Project, session: SessionMeta, base: str = "") -> str:
    knowledge = knowledge_for_inject(project)
    tree = workspace_tree_lines(project.workspace_path, max_entries=40)
    parts = [
        base.strip() or DEFAULT_ATELIER_POLICY,
        "",
        "---",
        f"# 当前项目: {project.name}",
        "",
        "## 项目说明 (KNOWLEDGE.md · 用户维护)",
        knowledge,
        "",
        "## Workspace 文件树（共享代码，跨会话）",
    ]
    if tree:
        parts.extend(f"- `{p}`" for p in tree)
        entries = {Path(p).name for p in tree}
        if "index.html" in entries:
            parts.append("- 入口提示: 打开 `/workspace/index.html` 验证 UI")
    else:
        parts.append("- （workspace 为空）")
    parts.append("")

    if session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 分支信息",
                f"你正在实验分支 `{session.branch_name or session.title}` 中。",
                "对话上下文与主会话隔离；**代码 workspace 与主会话共享**（先读再改）。",
                "实验完成后由用户 merge 或 discard。",
                "",
            ]
        )
        main_sum = _main_session_summary_line(project)
        if main_sum:
            parts.extend(
                [
                    "## 主会话摘要（仅供背景，非完整对话）",
                    main_sum,
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
    """Opt-in post-turn extract. Default enabled=False (no auto write)."""
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
    """Legacy sync helper. Default no-op unless enabled=True."""
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
    """Bind workspace + session + KNOWLEDGE / tree inject."""
    import dataclasses

    extra = build_system_prompt(project, session)
    return dataclasses.replace(
        base_settings,
        workspace=project.workspace_path,
        session_id=agent_session_id(project, session),
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
