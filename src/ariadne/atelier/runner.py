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

## 怎么干活（请照做）
1. 用户要改东西、加功能、画出来——请真的改文件并保存到 /workspace，别只看不改、别只在脑子里想完。
2. 做完后用大白话说清楚：改了啥、文件在哪、怎么打开看看（比如浏览器打开 index.html）。
3. 「画一只鸟 / 画出来」= 在现有小项目里加上能跑的功能（按钮、函数、小 demo），不是空口白话。
4. 能改现有文件就改现有的，别另起一套花里胡哨的工程。
5. 语气轻松一点，像热心朋友，少用术语；必要时才提工具名。

## 别拖太久（很重要）
6. **小步改**：一次只改一小段；单次写入尽量 < 150 行。大改拆成多轮，写完就告诉用户。
7. **禁止**把整份超长 JS/CSS 压成一行再重写——容易被截断，然后你会陷入「修→查→再修」死循环。
8. 检查 1～2 次够了；用户问「做好了吗」时，先回答现状（完成/未完成/卡在哪），不要继续空转检查。
9. 工具次数有限；快到上限时必须停下来用中文汇报。

## 小本本
用户可能有一份简短备忘（KNOWLEDGE.md）。当参考就好，别瞎编约定；细节靠对话记忆，不用每轮改备忘。
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
        f"# 现在在做：{project.name}",
        "",
        "## 小本本（想记啥就写啥）",
        knowledge,
        "",
        "## 文件夹里已有这些（大家共用同一份）",
    ]
    if tree:
        parts.extend(f"- `{p}`" for p in tree)
        entries = {Path(p).name for p in tree}
        if "index.html" in entries:
            parts.append("- 想预览的话：打开 index.html")
    else:
        parts.append("- （还是空的，可以一起从零开始）")
    parts.append("")

    if session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 旁支闲聊",
                f"你在旁支 `{session.branch_name or session.title}` 里试想法。",
                "聊天记录和主对话分开；**代码文件是同一份**，改了大家都能看见。",
                "试够了可以合并进主线，或者丢掉重来。",
                "",
            ]
        )
        main_sum = _main_session_summary_line(project)
        if main_sum:
            parts.extend(
                [
                    "## 主对话里大概在干嘛（随便看看）",
                    main_sum,
                    "",
                ]
            )

    parts.extend(
        [
            "## 目录",
            f"本机文件夹: {project.workspace_path}",
            "沙箱里叫: /workspace",
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
