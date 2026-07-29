"""KNOWLEDGE.md — atelier brief / 本坊便签 (Codex AGENTS.md style).

Design intent (see docs/design/atelier.md):
- **Primary value:** how **this 作坊** runs — 运作方式、关键路径、注意点.
- **Authoring:** user can always edit; **main** post-turn may small-step append
  clear operational 约定 (conservative heuristic). Branch never writes.
- **Not a second Memory:** chat details stay in Memory L0–L4.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .models import Project

# Keep template short — token cost is paid on every turn inject.
# Content focus: 本坊怎么运作 / 路径 / 注意 — not generic chat diary.
KNOWLEDGE_TEMPLATE = """# {name}

> 本坊便签：记**这间作坊怎么运作**——关键路径、怎么跑、注意什么。
> 主线聊定的会自动补几条；旁支只读。细节聊天靠 Memory。

## 本坊怎么运作
- （目标、流程、主线定策略 / 旁支动手…）
- 旁支遵守**本坊**便签的输出规范（不要套用别的作坊）

## 关键路径
- 工作区（沙箱可写）: `/workspace` → 本机会话目录（主线=`workspace/`，旁支=旁支树）
- 主线只读（仅旁支）: `/main-readonly` → 主线 `workspace/` 最新内容
- 本坊便签（权威）: 作坊根 `KNOWLEDGE.md`；沙箱内可读 `/workspace/KNOWLEDGE.md` 副本
- 旁支文件: `.ariadne/branch_workspaces/<旁支名>/`

## 输出规范
- （按本坊目标填写。例：画画→PNG/源码；网页→html；**仅**做架构分析时再写 md+svg）
- 需要展示的图写入 `/workspace`，用 `![说明](/workspace/…)` 嵌入
- 未约定的格式不要硬套（别的作坊习惯不作数）

## 注意
- （坑、约束、别写错路径、别动主线小本本…）
- 各作坊记忆与便签隔离，不要引用其它作坊的产物约定

## 随手记
- 
"""

# Section names accepted by apply_updates / auto-extract.
SECTION_NAMES = (
    "本坊怎么运作",
    "关键路径",
    "输出规范",
    "注意",
    "我想记住的",
    "随手记",
    "决策与约定",
    "备注",
    "技术栈",
    "关键决策",
    "约定",
    "经验教训",
    "进行中的工作",
)

# Soft cap for system inject (chars). Full file remains on disk for editing.
INJECT_CHAR_LIMIT = 4000


@dataclass
class KnowledgeUpdateItem:
    section: str
    type: str  # add | modify | remove
    old_text: str = ""
    new_text: str = ""
    evidence: str = ""


@dataclass
class KnowledgeUpdate:
    has_update: bool
    updates: list[KnowledgeUpdateItem] = field(default_factory=list)


def knowledge_template(name: str) -> str:
    return KNOWLEDGE_TEMPLATE.format(name=name)


def _workspace_knowledge_path(project: Project) -> Path:
    return project.workspace_path / "KNOWLEDGE.md"


def _looks_polluted(text: str) -> bool:
    """True when text looks like failed auto-extract / JSON dump."""
    s = text or ""
    junk_hits = sum(
        1
        for frag in ('"has_update"', '"updates"', '"section"', '"type": "add"', '"old_text"')
        if frag in s
    )
    return junk_hits >= 2


def _looks_scaffold(text: str) -> bool:
    """True for create/refresh scaffold drafts that should yield to a real rewrite.

    Agents often rewrite ``/workspace/KNOWLEDGE.md`` while the panel reads root;
    treating scaffolds as thin lets GET promote the workspace copy.
    """
    s = text or ""
    markers = (
        "扫了一眼文件夹",
        "小本本草稿",
        "本坊便签草稿（扫了一眼",
        "未能从文件树推断",
        "还没想好也可以先空着",
        "（目标与流程可手写）",
        "大概用了:",
        "扫到大约",
    )
    hits = sum(1 for m in markers if m in s)
    # Real handbooks are longer; scaffolds stay shortish
    return hits >= 1 and len(s.strip()) < 2500


def _looks_thin_or_polluted(text: str) -> bool:
    """True when root brief is empty shell, scaffold, or auto-extract garbage."""
    s = (text or "").strip()
    if not s:
        return True
    if _looks_polluted(s):
        return True
    if _looks_scaffold(s):
        return True
    if len(s) < 60:
        return True
    # mostly placeholders
    placeholders = s.count("（初始为空）") + s.count("[待确认]") + s.count("（在此记录")
    if placeholders >= 3 and len(s) < 600:
        return True
    return False


def _workspace_richer_than_root(root: str, ws: str) -> bool:
    """True when workspace copy should replace root (agent rewrote via /workspace)."""
    ws_s = (ws or "").strip()
    root_s = (root or "").strip()
    if not ws_s or _looks_polluted(ws_s):
        return False
    # Polluted root always yields to any clean workspace notes.
    if _looks_polluted(root_s):
        return True
    if not root_s or _looks_thin_or_polluted(root_s):
        # Prefer workspace even if short, as long as it has real substance
        if not _looks_scaffold(ws_s) and not _looks_polluted(ws_s):
            return len(ws_s) >= 40 or len(ws_s) > len(root_s)
        return len(ws_s) > len(root_s)
    # Root looks ok but workspace is a substantially fuller handbook
    ops_markers = (
        "本坊怎么运作",
        "关键路径",
        "作坊结构",
        "路径速查",
        "注意事项",
        "/workspace",
    )
    ws_ops = sum(1 for m in ops_markers if m in ws_s)
    root_ops = sum(1 for m in ops_markers if m in root_s)
    if ws_ops >= 2 and len(ws_s) >= len(root_s) + 150 and ws_ops >= root_ops:
        return True
    return False


def read_knowledge(project: Project) -> str:
    """Canonical project brief at atelier root (user-edited)."""
    if project.knowledge_path.is_file():
        return project.knowledge_path.read_text(encoding="utf-8")
    return knowledge_template(project.name)


def knowledge_for_inject(project: Project, *, limit: int = INJECT_CHAR_LIMIT) -> str:
    """Text injected into system prompt (truncated).

    Prefer root KNOWLEDGE.md; if workspace/KNOWLEDGE.md is a richer handbook
    (e.g. agent rewrote via sandbox), inject that (GET may promote to root).
    """
    # Promote first so inject and panel stay aligned.
    sync_knowledge_from_workspace_if_empty(project)
    text = read_knowledge(project).strip() or knowledge_template(project.name).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n…(截断；请精简 KNOWLEDGE.md)"


def sync_knowledge_from_workspace_if_empty(project: Project) -> bool:
    """Promote workspace/KNOWLEDGE.md → root when it is a richer handbook.

    Agents often write ``/workspace/KNOWLEDGE.md``; the panel reads root. This
    bridges the two. Returns True when root was rewritten (history snapshotted).
    """
    root = ""
    if project.knowledge_path.is_file():
        try:
            root = project.knowledge_path.read_text(encoding="utf-8")
        except OSError:
            root = ""
    ws_path = _workspace_knowledge_path(project)
    if not ws_path.is_file():
        return False
    try:
        ws = ws_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not _workspace_richer_than_root(root, ws):
        return False
    write_knowledge(project, ws, session_id="sync-workspace", mirror_workspace=False)
    return True


def write_knowledge(
    project: Project,
    content: str,
    *,
    session_id: str = "system",
    mirror_workspace: bool = True,
) -> None:
    """Write authoritative root KNOWLEDGE.md; optionally mirror under workspace/."""
    project.knowledge_history_dir.mkdir(parents=True, exist_ok=True)
    if project.knowledge_path.is_file():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = project.knowledge_history_dir / f"{ts}.md"
        shutil.copy2(project.knowledge_path, dest)
    body = content.rstrip() + "\n"
    project.knowledge_path.write_text(body, encoding="utf-8")
    if mirror_workspace:
        try:
            wp = _workspace_knowledge_path(project)
            wp.parent.mkdir(parents=True, exist_ok=True)
            wp.write_text(body, encoding="utf-8")
        except OSError:
            pass
    _ = session_id


def ensure_knowledge_in_session_workspace(project: Project, session: Any) -> Path | None:
    """Copy root 便签 into this session's /workspace/KNOWLEDGE.md for sandbox tools.

    Branch sandboxes only mount ``branch_workspaces/<slug>/``, so agents cannot
    read the atelier-root brief via host absolute paths (escape) or a missing
    ``/workspace/KNOWLEDGE.md``. Refresh from root each call so branches see
    the latest main handbook (branch edits to this file do **not** promote).
    """
    from .models import SessionMeta, SessionType  # local to avoid cycles at import

    if not isinstance(session, SessionMeta):
        return None
    # Always read authoritative root (after optional main-workspace promote)
    if session.type == SessionType.MAIN:
        sync_knowledge_from_workspace_if_empty(project)
    text = read_knowledge(project).strip()
    if not text:
        text = knowledge_template(project.name).strip()
    body = text if text.endswith("\n") else text + "\n"
    dest = project.session_workspace(session) / "KNOWLEDGE.md"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Branch: always refresh from root. Main: write if missing or root changed.
        if session.type == SessionType.BRANCH or not dest.is_file():
            dest.write_text(body, encoding="utf-8")
        else:
            try:
                cur = dest.read_text(encoding="utf-8")
            except OSError:
                cur = ""
            if cur != body:
                dest.write_text(body, encoding="utf-8")
        return dest
    except OSError:
        return None


def list_knowledge_history(project: Project) -> list[Path]:
    d = project.knowledge_history_dir
    if not d.is_dir():
        return []
    return sorted(d.glob("*.md"), reverse=True)


def _scan_tree(root: Path, *, max_entries: int = 80) -> list[str]:
    if not root.is_dir():
        return []
    out: list[str] = []
    skip = {".git", ".venv", "node_modules", "__pycache__", ".ariadne", ".pytest_cache"}
    root = root.resolve()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel_parts = p.resolve().relative_to(root).parts
        except ValueError:
            continue
        # Only skip dirs *inside* the workspace tree — do not match host parents
        # named .ariadne (web ateliers live under ~/.…/.ariadne/…/workspace).
        if any(part in skip for part in rel_parts[:-1]):
            continue
        if rel_parts and rel_parts[0] in skip:
            continue
        out.append(Path(*rel_parts).as_posix() if len(rel_parts) > 1 else rel_parts[0])
        if len(out) >= max_entries:
            break
    return out


def workspace_tree_lines(workspace: Path, *, max_entries: int = 40) -> list[str]:
    """Relative file paths under workspace for system inject."""
    return _scan_tree(workspace, max_entries=max_entries)


def heuristic_refresh(project: Project) -> str:
    """One-shot scaffold from file tree / README (create / explicit refresh only)."""
    files = _scan_tree(project.workspace_path)
    stack: list[str] = []
    if any(f.endswith(".py") for f in files):
        stack.append("Python")
    if "pyproject.toml" in files or "requirements.txt" in files:
        stack.append("Python packaging")
    if any(f.endswith((".ts", ".tsx", ".js", ".jsx")) for f in files):
        stack.append("JavaScript/TypeScript")
    if "package.json" in files:
        stack.append("Node.js")
    if "Dockerfile" in files or "docker-compose.yml" in files:
        stack.append("Docker")
    if "Cargo.toml" in files:
        stack.append("Rust")
    if not stack:
        stack.append("（未能从文件树推断，请手写）")

    note = ""
    for cand in ("README.md", "README.zh-CN.md", "readme.md"):
        p = project.workspace_path / cand
        if p.is_file():
            first = next(
                (ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()),
                "",
            )
            note = first[:120]
            break

    sample = ", ".join(files[:8]) if files else "（还空）"
    body = f"""# {project.name}

> 本坊便签草稿（扫了一眼文件夹）：记**怎么运作 / 路径 / 注意**，不是聊天流水。

## 本坊怎么运作
- 栈/技术: {', '.join(stack)}
{f'- README: {note}' if note else '- （目标与流程可手写）'}

## 关键路径
- 可写工作区: `/workspace`（主线=`workspace/`，旁支=旁支树）
- 主线只读（旁支）: `/main-readonly` → 主线最新 `workspace/`
- 本坊便签: 作坊根 `KNOWLEDGE.md`；沙箱副本 `/workspace/KNOWLEDGE.md`
- 旁支目录: `.ariadne/branch_workspaces/<名>/`
- 当前扫到的文件示例: {sample}

## 注意
- 改项目文件写 `/workspace`；改便签用本坊便签面板（或主线自动约定），别假定沙箱能写根目录便签

## 随手记
- 约 {len(files)} 个文件 · {time.strftime('%Y-%m-%d')}
"""
    return body


def _section_bounds(text: str, section: str) -> tuple[int, int, int] | None:
    pattern = re.compile(rf"(?m)^##\s+{re.escape(section)}\s*$")
    m = pattern.search(text)
    if not m:
        return None
    body_start = m.end()
    nxt = re.search(r"(?m)^##\s+", text[body_start:])
    body_end = body_start + nxt.start() if nxt else len(text)
    return m.end(), body_start, body_end


def _ensure_bullet(line: str) -> str:
    s = line.strip()
    if not s:
        return s
    if not s.startswith("-"):
        return "- " + s
    return s


def apply_updates(content: str, update: KnowledgeUpdate) -> str:
    """Programmatic section ops (API / tests). UI prefers full-file edit."""
    if not update.has_update or not update.updates:
        return content
    text = content
    for item in update.updates:
        op = (item.type or "add").strip().lower()
        section = (item.section or "我想记住的").strip()

        bounds = _section_bounds(text, section)
        if op == "add":
            line = _ensure_bullet(item.new_text)
            if not line:
                continue
            if bounds is None:
                text = text.rstrip() + f"\n\n## {section}\n{line}\n"
                continue
            _, body_start, _ = bounds
            text = text[:body_start] + "\n" + line + text[body_start:]
            continue

        if bounds is None:
            continue
        _, body_start, body_end = bounds
        body = text[body_start:body_end]
        lines = body.splitlines(keepends=True)

        if op == "remove":
            target = (item.old_text or item.new_text or "").strip().lstrip("-* ").strip()
            if not target:
                continue
            new_lines = [ln for ln in lines if target.lower() not in ln.lower()]
            text = text[:body_start] + "".join(new_lines) + text[body_end:]
            continue

        if op == "modify":
            old = (item.old_text or "").strip().lstrip("-* ").strip()
            new = _ensure_bullet(item.new_text)
            if not old or not new:
                continue
            replaced = False
            new_lines = []
            for ln in lines:
                if not replaced and old.lower() in ln.lower():
                    nl = "\n" if ln.endswith("\n") else ""
                    new_lines.append(new + nl)
                    replaced = True
                else:
                    new_lines.append(ln)
            if not replaced:
                new_lines.insert(0, new + ("\n" if new_lines else "\n"))
            text = text[:body_start] + "".join(new_lines) + text[body_end:]
            continue

    return text


# ── Post-turn 约定 extract (main path; conservative) ───────────────────────

# Prefer operational section; legacy "我想记住的" / "决策与约定" still ok.
DEFAULT_BRIEF_SECTION = "本坊怎么运作"
PATH_BRIEF_SECTION = "关键路径"
NOTE_BRIEF_SECTION = "注意"

_AGREE_SIGNAL = re.compile(
    r"(?i)("
    r"我们决定|就这么定|就定|决定使用|决定用|采用|约定|定为|定了|"
    r"prefer|we (decided|will use|should use)|always use|"
    r"记住|别忘了|以后都|统一用|只用|必须用|"
    r"不要再|不再使用|改为|改用|改成|改成用|"
    r"用\s*[\w\u4e00-\u9fff.+\-]{1,40}\s*(做|当|实现)|"
    r"路径|目录|workspace|/workspace|入口|怎么跑|如何启动|注意|坑|"
    r"写到|放到|保存在|输出到"
    r")"
)
_PATH_SIGNAL = re.compile(
    r"(?i)(/workspace|workspace/|路径|目录|KNOWLEDGE\.md|branch_workspaces|入口|index\.html)"
)
_NOTE_SIGNAL = re.compile(r"(?i)(注意|别|不要|禁止|坑|小心|务必|必须)")
_REMOVE_SIGNAL = re.compile(
    r"(?i)(废弃|删除约定|不再使用|不要再用|取消约定|remove decision|stop using)"
)
_NOISE_LINE = re.compile(
    r"(?i)^("
    r"好的|嗯|哈哈|谢谢|请|帮我|你觉得|怎么样|如何|吗\??|？|\?|"
    r"ok|okay|thanks|please|hello|hi"
    r")[\s!！.。]*$"
)


def _core_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lstrip("-*• ").strip()).lower()


def _line_looks_like_agreement(clean: str) -> bool:
    if len(clean) < 10 or len(clean) > 180:
        return False
    if _NOISE_LINE.match(clean):
        return False
    if clean.endswith("?") or clean.endswith("？"):
        return False
    if clean.count("{") >= 2 or '"has_update"' in clean:
        return False
    return bool(_AGREE_SIGNAL.search(clean))


def extract_knowledge_heuristic(conversation: list[dict[str, Any]]) -> KnowledgeUpdate:
    """Conservative keyword extract for durable 约定 (main post-turn path)."""
    updates: list[KnowledgeUpdateItem] = []
    seen: set[str] = set()
    for row in conversation:
        if row.get("role") not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "")
        for line in content.splitlines():
            clean = line.strip()
            if not _line_looks_like_agreement(clean):
                continue
            core = _core_text(clean)
            if not core or core in seen:
                continue
            seen.add(core)
            body = clean.lstrip("-*• ").strip()
            if _REMOVE_SIGNAL.search(clean):
                updates.append(
                    KnowledgeUpdateItem(
                        section=DEFAULT_BRIEF_SECTION,
                        type="remove",
                        old_text=body,
                        evidence=clean[:120],
                    )
                )
            else:
                if _PATH_SIGNAL.search(clean):
                    section = PATH_BRIEF_SECTION
                elif _NOTE_SIGNAL.search(clean):
                    section = NOTE_BRIEF_SECTION
                else:
                    section = DEFAULT_BRIEF_SECTION
                updates.append(
                    KnowledgeUpdateItem(
                        section=section,
                        type="add",
                        new_text=body,
                        evidence=clean[:120],
                    )
                )
    return KnowledgeUpdate(has_update=bool(updates), updates=updates[:5])


def filter_knowledge_update(
    current: str,
    update: KnowledgeUpdate,
    *,
    max_ops: int = 3,
) -> KnowledgeUpdate:
    """Drop no-ops / dupes / junk before write. Small-step only."""
    if not update.has_update or not update.updates:
        return KnowledgeUpdate(has_update=False)
    cur_l = (current or "").lower()
    out: list[KnowledgeUpdateItem] = []
    for item in update.updates:
        op = (item.type or "add").strip().lower()
        if op not in {"add", "modify", "remove"}:
            op = "add"
        section = (item.section or DEFAULT_BRIEF_SECTION).strip() or DEFAULT_BRIEF_SECTION
        if section not in SECTION_NAMES:
            section = DEFAULT_BRIEF_SECTION
        new_t = (item.new_text or "").strip()
        old_t = (item.old_text or "").strip()
        if op == "add":
            core = _core_text(new_t)
            if len(core) < 8 or len(new_t) > 180:
                continue
            if core in cur_l:
                continue
            if _looks_polluted(new_t):
                continue
            out.append(
                KnowledgeUpdateItem(
                    section=section,
                    type="add",
                    new_text=new_t.lstrip("-*• ").strip(),
                    evidence=(item.evidence or "")[:120],
                )
            )
        elif op == "remove":
            target = _core_text(old_t or new_t)
            if len(target) < 4 or target not in cur_l:
                continue
            out.append(
                KnowledgeUpdateItem(
                    section=section,
                    type="remove",
                    old_text=(old_t or new_t).lstrip("-*• ").strip(),
                    evidence=(item.evidence or "")[:120],
                )
            )
        else:
            old_c = _core_text(old_t)
            new_c = _core_text(new_t)
            if len(old_c) < 4 or len(new_c) < 8 or len(new_t) > 180:
                continue
            if old_c not in cur_l:
                if new_c and new_c not in cur_l and not _looks_polluted(new_t):
                    out.append(
                        KnowledgeUpdateItem(
                            section=section,
                            type="add",
                            new_text=new_t.lstrip("-*• ").strip(),
                            evidence=(item.evidence or "")[:120],
                        )
                    )
                continue
            if _looks_polluted(new_t):
                continue
            out.append(
                KnowledgeUpdateItem(
                    section=section,
                    type="modify",
                    old_text=old_t.lstrip("-*• ").strip(),
                    new_text=new_t.lstrip("-*• ").strip(),
                    evidence=(item.evidence or "")[:120],
                )
            )
        if len(out) >= max_ops:
            break
    return KnowledgeUpdate(has_update=bool(out), updates=out)


def content_safe_to_write(text: str) -> bool:
    """Reject polluted auto-extract dumps as the whole brief body."""
    if _looks_polluted(text or ""):
        return False
    return True



def _parse_llm_json(raw: str) -> KnowledgeUpdate:
    text = (raw or "").strip()
    if not text or text.upper() == "NONE":
        return KnowledgeUpdate(has_update=False)
    if "```" in text:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if m:
            text = m.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        lines = [ln.strip().lstrip("- ") for ln in text.splitlines() if ln.strip()]
        if not lines or lines[0].upper() == "NONE":
            return KnowledgeUpdate(has_update=False)
        return KnowledgeUpdate(
            has_update=True,
            updates=[
                KnowledgeUpdateItem(section="决策与约定", type="add", new_text=ln)
                for ln in lines[:5]
            ],
        )
    if not isinstance(data, dict) or not data.get("has_update"):
        return KnowledgeUpdate(has_update=False)
    updates: list[KnowledgeUpdateItem] = []
    for u in data.get("updates") or []:
        if not isinstance(u, dict):
            continue
        op = str(u.get("type") or "add").lower()
        if op not in {"add", "modify", "remove"}:
            op = "add"
        section = str(u.get("section") or "决策与约定").strip()
        updates.append(
            KnowledgeUpdateItem(
                section=section,
                type=op,
                old_text=str(u.get("old_text") or ""),
                new_text=str(u.get("new_text") or ""),
                evidence=str(u.get("evidence") or "")[:200],
            )
        )
    return KnowledgeUpdate(has_update=bool(updates), updates=updates[:8])


async def extract_knowledge_llm(
    conversation: list[dict[str, Any]],
    current_knowledge: str,
    *,
    complete: Callable[[str], Awaitable[str]] | None = None,
) -> KnowledgeUpdate:
    """Opt-in LLM extract. Not wired into default turn / merge paths."""
    if complete is None:
        return extract_knowledge_heuristic(conversation)
    dialogue = "\n".join(
        f"{r.get('role')}: {r.get('content')}" for r in conversation[-8:]
    )
    prompt = f"""分析对话，提取对项目说明（KNOWLEDGE.md）的更新。只输出 JSON。

当前文件:
{current_knowledge[:4000]}

最近对话:
{dialogue}

JSON:
{{"has_update": true/false, "updates": [
  {{"section": "决策与约定|备注", "type": "add|modify|remove",
    "old_text": "", "new_text": "", "evidence": ""}}
]}}
仅提取明确决策/约定；无则 has_update=false。
"""
    try:
        raw = await complete(prompt)
    except Exception:
        return extract_knowledge_heuristic(conversation)
    return _parse_llm_json(raw)


def make_llm_complete(settings: Any) -> Callable[[str], Awaitable[str]] | None:
    base = getattr(settings, "base_url", "") or ""
    key = getattr(settings, "api_key", "") or ""
    model = getattr(settings, "model", "") or ""
    if not base or not key:
        return None

    async def complete(prompt: str) -> str:
        from ..model.openai_chat import OpenAIChatModel

        m = OpenAIChatModel(base_url=base, api_key=key, model=model)
        exchange = await m.complete(
            messages=[
                {
                    "role": "system",
                    "content": "You extract structured project notes. Reply with JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
            tool_choice=None,
            temperature=0.1,
            max_tokens=1024,
        )
        return str(exchange.message.content or "")

    return complete


def generate_branch_summary(transcript: list[dict[str, Any]], *, branch_name: str) -> str:
    """Short human-readable merge note (appended only when user merges)."""
    users = [str(r.get("content") or "")[:200] for r in transcript if r.get("role") == "user"]
    asst = [str(r.get("content") or "")[:200] for r in transcript if r.get("role") == "assistant"]
    lines = [
        f"## 分支合并：`{branch_name}`",
        f"- 用户轮次: {len(users)} · 助手轮次: {len(asst)}",
    ]
    if users:
        lines.append(f"- 首条意图: {users[0][:160]}")
    if asst:
        lines.append(f"- 末条摘要: {asst[-1][:160]}")
    lines.append("- （有用的话抄进小本本，这段可以删）")
    return "\n".join(lines) + "\n"
