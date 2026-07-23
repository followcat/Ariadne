"""Atelier session: inject user-owned KNOWLEDGE.md + workspace snapshot.

Auto-extract after turns is **off by default** (Codex AGENTS.md model).
Delivery policy steers implement tasks to write files and non-empty replies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .knowledge import knowledge_for_inject, read_knowledge, workspace_tree_lines
from .models import Project, SessionMeta, SessionType, append_transcript


DEFAULT_ATELIER_POLICY = """你在 Ariadne 的「小作坊」里陪用户一起捣鼓项目（画画、写小网页、试想法都可以）。

## 主线 vs 旁支（隔离！）
- **主线 (main)**：策略、工作定义、取舍。绑定 **主线自己的文件夹**。
- **旁支 (branch-*)**：**独立文件夹 + 独立记忆**。创建时从主线拷贝一份快照，之后改什么都**不会**动主线文件。
- 旁支的聊天/记忆也和主线分开；「收」只归档旁支摘要，默认**不写回**主线文件或小本本。
- 主线对话内容不受旁支污染。

## 怎么干活（请照做）
1. 动手时写当前会话的 /workspace（旁支=旁支目录，主线=主线目录），别只看不改。
2. 做完说清楚改了啥、怎么打开看。
3. **出图**：PNG 写到当前 /workspace，并用 `![说明](/workspace/xxx.png)` 展示。
4. 语气轻松；小步改；别一次重写超大文件。

## 小本本
主线的 KNOWLEDGE.md 只作参考；旁支不要去改主线小本本。
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
    ws = project.session_workspace(session)
    tree = workspace_tree_lines(ws, max_entries=40)
    parts = [
        base.strip() or DEFAULT_ATELIER_POLICY,
        "",
        "---",
        f"# 现在在做：{project.name}",
        "",
        "## 小本本（主线参考）",
        knowledge,
        "",
        "## 当前会话文件夹里有什么",
    ]
    if tree:
        parts.extend(f"- `{p}`" for p in tree)
        entries = {Path(p).name for p in tree}
        if "index.html" in entries:
            parts.append("- 想预览的话：打开 index.html")
    else:
        parts.append("- （还是空的，可以一起从零开始）")
    parts.append("")

    if session.type == SessionType.MAIN:
        parts.extend(
            [
                "## 当前：主线（独立空间）",
                "策略、工作定义、取舍。写文件只会改主线文件夹。",
                "大段实现建议开旁支；旁支改不到主线。",
                "",
            ]
        )
    elif session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 当前：旁支（独立空间）",
                f"旁支名：`{session.branch_name or session.title}`。",
                "这是主线的**拷贝**，聊天/记忆/文件都独立；**禁止假设能改到主线**。",
                "出图写入本旁支 /workspace，并用 `![说明](/workspace/文件.png)` 展示。",
                "",
            ]
        )
        main_sum = _main_session_summary_line(project)
        if main_sum:
            parts.extend(
                [
                    "## 主线策略摘要（只读背景）",
                    main_sum,
                    "",
                ]
            )

    parts.extend(
        [
            "## 目录",
            f"本机文件夹: {ws}",
            "沙箱里叫: /workspace",
            f"会话类型: {session.type.value}",
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
    """Bind **session-scoped** workspace + data_dir + KNOWLEDGE/tree inject.

    Main → project.workspace_path + project.data_dir  
    Branch → isolated branch_workspaces/<slug> + scopes/<session.id>
    """
    import dataclasses

    extra = build_system_prompt(project, session)
    # Workshop implements more often write mid-size files → 16k completion budget.
    atelier_max_tokens = max(int(getattr(base_settings, "max_tokens", 8192) or 8192), 16384)
    return dataclasses.replace(
        base_settings,
        workspace=project.session_workspace(session),
        session_id=agent_session_id(project, session),
        data_dir=project.session_data_dir(session),
        tool_loop_limit=project.config.max_tool_loop,
        sandbox_profile=project.config.sandbox_profile,
        docker_image=project.config.docker_image,
        sandbox_network=project.config.network_mode,
        extra_system_prompt=extra,
        max_tokens=atelier_max_tokens,
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
