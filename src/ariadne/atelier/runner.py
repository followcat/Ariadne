"""Atelier session: inject KNOWLEDGE.md (便签) + workspace snapshot.

Main post-turn may small-step update 便签 when clear 约定 appear (default on).
Branch never writes the brief. Delivery policy steers implement tasks to write
files and non-empty replies.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .knowledge import knowledge_for_inject, read_knowledge, workspace_tree_lines
from .models import Project, SessionMeta, SessionType, append_transcript


DEFAULT_ATELIER_POLICY = """你在 Ariadne 的「小作坊」里陪用户一起捣鼓项目（画画、写小网页、试想法都可以）。

## 主线 vs 旁支（隔离！）
- **主线 (main)**：策略、工作定义、取舍、本坊交付约定。绑定 **主线自己的文件夹** → `/workspace`。
- **旁支 (branch-*)**：**独立可写** `/workspace`（创建时从主线快照）；**只读** 主线最新树在 **`/main-readonly`**。
- 旁支聊天/记忆独立；「收」只归档旁支摘要，默认不写回主线文件或小本本。
- **旁支遵守本坊便签的输出规范**（只读当前作坊便签，不要套用别的作坊习惯）。

## 怎么干活（请照做）
1. **写**只往当前 `/workspace`（旁支盘）；对照主线最新文件读 **`/main-readonly/...`**。
2. 做完说清楚改了啥、产物路径、怎么打开看。
3. **落盘优先**：需要留下的结果写成 `/workspace` 文件；图片用 `![说明](/workspace/….png|svg)` 展示。
4. 语气轻松；小步改；别一次重写超大文件。
5. **交付格式以本坊便签为准**。便签没写死时，按用户本轮任务来（画画→画/PNG；写网页→html；架构分析→再考虑 md/svg 等）。

## 小本本（本坊便签）
记**这间作坊怎么运作**：关键路径、怎么跑、注意点、输出规范（不是聊天流水账，也不是别坊的记忆）。
- **system 里已注入本坊便签全文**；沙箱内可读 **`/workspace/KNOWLEDGE.md`**（本坊权威副本）。
- **禁止**用本机绝对路径读便签。
- 旁支不要改便签权威；项目文件写 `/workspace` 其它路径。细节回忆靠 **本会话 Memory**（不作坊互通）。
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


def _extract_output_spec(knowledge: str) -> str | None:
    """Pull 输出规范 / 交付 section from 便签 for a hard branch reminder.

    Only starts on a heading-like line so inline mentions (e.g. table cells)
    do not open a false block.
    """
    text = knowledge or ""
    if not text.strip():
        return None
    lines = text.splitlines()
    captured: list[str] = []
    in_block = False
    # "### 输出规范（旁支必做）" / "## 输出规范" — not table cells
    heading_re = re.compile(
        r"^(?:#{1,4}\s+).{0,20}?(?:输出规范|交付物|交付清单|默认交付物?)"
    )
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if heading_re.search(stripped) and not stripped.startswith("|"):
                in_block = True
                captured.append(stripped)
            continue
        # stop at next same-or-higher markdown heading
        if re.match(r"^##\s+", stripped) and not re.search(
            r"输出规范|交付", stripped
        ):
            break
        if stripped.startswith("---") and len(captured) > 1:
            break
        # stop before a new emoji-led top section (## 🧰 etc already handled)
        captured.append(line.rstrip())
        if len("\n".join(captured)) > 900:
            break
    if not captured:
        bullets = [
            ln.strip()
            for ln in lines
            if re.search(r"\.svg|architecture\.md|架构图|架构描述", ln)
            and (
                ln.strip().startswith(("-", "*", ">"))
                or (ln.strip().startswith("|") and "svg" in ln.lower())
            )
        ]
        if bullets:
            captured = bullets[:12]
    if not captured:
        return None
    body = "\n".join(captured).strip()
    return body[:1000] if body else None


def _knowledge_implies_architecture_delivery(knowledge: str) -> bool:
    """True only when THIS atelier's 输出规范 explicitly requires architecture products.

    Soft mentions like「仅架构分析时再写 md+svg」in the default template do **not** count —
    that was causing painting ateliers to hard-require architecture.md/SVG.
    """
    spec = _extract_output_spec(knowledge) or ""
    if not spec.strip():
        return False
    # Soft / optional wording → not a hard requirement for every turn
    if re.search(r"仅.{0,40}架构|架构分析时再|可选|不要默认", spec):
        # Still allow if they also name the concrete product files as required
        if not re.search(r"architecture\.md|旁支必做|必须", spec, re.I):
            return False
    if re.search(r"architecture\.md", spec, re.I):
        return True
    if re.search(r"架构图", spec) and re.search(r"\.svg|\bsvg\b", spec, re.I):
        return True
    if re.search(r"架构描述", spec) and re.search(r"\.md|\bmd\b", spec, re.I):
        return True
    return False


def _branch_deliverable_block(project: Project, knowledge: str) -> list[str]:
    """Branch delivery hints scoped to *this* atelier's 便签 — never global architecture defaults."""
    block = [
        "## 旁支交付（跟本坊便签）",
        "只服务**当前作坊**的目标；不要把别的作坊（例如架构分析坊）的交付习惯搬过来。",
        "产物写到本旁支 `/workspace`；对照主线最新文件读 `/main-readonly/`（只读）。",
        "交付格式：**本坊便签「输出规范」优先**；便签未写则按用户本轮任务决定文件类型。",
    ]
    spec = _extract_output_spec(knowledge)
    wants_arch = _knowledge_implies_architecture_delivery(knowledge)
    if spec:
        block.extend(["", "### 本坊便签 · 输出规范（权威摘录）", spec])
    if wants_arch:
        block.extend(
            [
                "",
                "### 本坊要求架构类交付",
                "1. `{项目}-architecture.md` — 架构描述",
                "2. 至少一张 `{项目}-…-architecture.svg`（若便签要求）",
                "3. 回复中用 `![说明](/workspace/…)` 展示",
            ]
        )
    else:
        block.extend(
            [
                "",
                "### 本坊未强制架构交付",
                "不要默认产出 `*-architecture.md` / 架构 SVG。",
                "画画/创作坊：画、PNG、源码即可；用户没要架构图就不要硬做。",
            ]
        )
    main_tree = workspace_tree_lines(project.workspace_path, max_entries=24)
    if main_tree:
        block.append("")
        block.append("### 主线 `/main-readonly` 当前文件（只读对照）")
        block.extend(f"- `{p}`" for p in main_tree)
    return block


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
        "## 本坊便签（怎么运作 / 路径 / 注意 / 输出规范 · 主线权威）",
        knowledge,
        "",
        "## 当前会话文件夹里有什么",
    ]
    if tree:
        parts.extend(f"- `{p}`" for p in tree)
        entries = {Path(p).name for p in tree}
        if "index.html" in entries:
            parts.append("- 想预览的话：打开 index.html")
        # Nudge when leftover demo / foreign-format files remain
        foreign = [
            p
            for p in tree
            if "cohersoup" in p.lower()
            or (
                "architecture" in p.lower()
                and not _knowledge_implies_architecture_delivery(knowledge)
            )
        ]
        if foreign and session.type == SessionType.BRANCH:
            parts.append(
                "- ⚠ 上列若含其它任务遗留（如 cohersoup / 误生成的 architecture 文件），"
                "不要当成本任务必交付；按本坊便签与用户本轮目标来。"
            )
    else:
        parts.append("- （还是空的，可以一起从零开始）")
    parts.append("")

    if session.type == SessionType.MAIN:
        parts.extend(
            [
                "## 当前：主线（独立空间）",
                "策略、工作定义、取舍、本坊输出规范。写文件只会改主线文件夹。",
                "大段实现 / 试验建议开旁支；旁支改不到主线。",
                "交付约定写在本坊便签；只对本坊旁支生效，不作坊互通。",
                "",
            ]
        )
    elif session.type == SessionType.BRANCH:
        parts.extend(
            [
                "## 当前：旁支（独立空间）",
                f"旁支名：`{session.branch_name or session.title}`。",
                "- **可写** `/workspace` = 旁支自己的文件树（创建时从主线快照）。",
                "- **只读** `/main-readonly` = **主线当前** workspace（实时可读，禁止写入）。",
                "- 聊天/记忆与主线隔离；不要假设写 `/workspace` 会改到主线。",
                "",
            ]
        )
        parts.extend(_branch_deliverable_block(project, knowledge))
        parts.append("")
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
    enabled: bool = True,
) -> dict[str, Any]:
    """Main post-turn: small-step 约定 → 便签 when present (default on).

    Branch sessions never write. Casual turns noop. Never whole-file free rewrite.
    """
    if not enabled:
        return {"updated": False, "reason": "disabled", "source": "none"}
    if session.type != SessionType.MAIN:
        return {"updated": False, "reason": "branch_skip", "source": "none"}

    from .knowledge import (
        apply_updates,
        content_safe_to_write,
        extract_knowledge_heuristic,
        extract_knowledge_llm,
        filter_knowledge_update,
        make_llm_complete,
        write_knowledge,
    )

    conv = [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": assistant_text},
    ]
    from .knowledge import sync_knowledge_from_workspace_if_empty

    # Agent may have sandbox-written /workspace/KNOWLEDGE.md this turn — promote first.
    synced = sync_knowledge_from_workspace_if_empty(project)

    complete = make_llm_complete(settings) if use_llm and settings is not None else None
    if complete is not None:
        update = await extract_knowledge_llm(
            conv, read_knowledge(project), complete=complete
        )
        source = "llm"
    else:
        update = extract_knowledge_heuristic(conv)
        source = "heuristic"
    current = read_knowledge(project)
    update = filter_knowledge_update(current, update)
    if not update.has_update:
        return {
            "updated": bool(synced),
            "reason": "synced_workspace" if synced else "no_update",
            "source": "sync" if synced else source,
        }
    new_content = apply_updates(current, update)
    if new_content == current:
        return {"updated": False, "reason": "noop", "source": source}
    if not content_safe_to_write(new_content):
        return {"updated": False, "reason": "polluted", "source": source}
    write_knowledge(project, new_content, session_id=session.id)
    # Also pick up any agent rewrite under /workspace/KNOWLEDGE.md this turn.
    from .knowledge import sync_knowledge_from_workspace_if_empty

    sync_knowledge_from_workspace_if_empty(project)
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
    enabled: bool = True,
) -> bool:
    """Sync helper for hosts without async. Same gates as async path (heuristic)."""
    if not enabled:
        return False
    if session.type != SessionType.MAIN:
        return False
    from .knowledge import (
        apply_updates,
        content_safe_to_write,
        extract_knowledge_heuristic,
        filter_knowledge_update,
        write_knowledge,
    )

    update = extract_knowledge_heuristic(
        [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": assistant_text},
        ]
    )
    current = read_knowledge(project)
    update = filter_knowledge_update(current, update)
    if not update.has_update:
        return False
    new_content = apply_updates(current, update)
    if new_content == current:
        return False
    if not content_safe_to_write(new_content):
        return False
    write_knowledge(project, new_content, session_id=session.id)
    return True


def settings_for_atelier(project: Project, session: SessionMeta, base_settings: Any) -> Any:
    """Bind **session-scoped** workspace + data_dir + KNOWLEDGE/tree inject.

    Main → project.workspace_path + project.data_dir  
    Branch → isolated branch_workspaces/<slug> + scopes/<session.id>

    Always materialize ``/workspace/KNOWLEDGE.md`` from root brief so tools can
    read the 便签 without host absolute paths (esp. on branches).
    """
    import dataclasses

    from .knowledge import ensure_knowledge_in_session_workspace

    ensure_knowledge_in_session_workspace(project, session)
    extra = build_system_prompt(project, session)
    # Workshop implements more often write mid-size files → 16k completion budget.
    atelier_max_tokens = max(int(getattr(base_settings, "max_tokens", 8192) or 8192), 16384)
    # Branch: expose live main workspace as read-only /main-readonly
    main_ro = None
    if session.type == SessionType.BRANCH:
        main_ro = project.workspace_path
    return dataclasses.replace(
        base_settings,
        workspace=project.session_workspace(session),
        main_readonly_workspace=main_ro,
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
