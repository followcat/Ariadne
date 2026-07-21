"""KNOWLEDGE.md — user-owned project brief (Codex AGENTS.md style).

Design intent (see docs/design/atelier.md):
- **Primary value:** cross-session project continuity via always-injected markdown.
- **Authoring:** user-led. Automatic extract is optional/legacy, not the default path.
- **Not a second Memory:** turn-level recall stays in Memory L0–L4; this file is a
  short, durable project card.
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
KNOWLEDGE_TEMPLATE = """# {name}

> 这是小本本：想到啥写啥就行，不用很正式。
> 下次接着聊时，我还能看到这里。

## 我想记住的
- （比如：用什么颜色、做什么效果、踩过什么坑…）

## 随手记
- 
"""

# Legacy section names still accepted by apply_updates helpers.
SECTION_NAMES = (
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


def _looks_thin_or_polluted(text: str) -> bool:
    """True when root brief is empty shell or auto-extract garbage."""
    s = (text or "").strip()
    if not s:
        return True
    if _looks_polluted(s):
        return True
    if len(s) < 60:
        return True
    # mostly placeholders
    placeholders = s.count("（初始为空）") + s.count("[待确认]") + s.count("（在此记录")
    if placeholders >= 3 and len(s) < 600:
        return True
    return False


def read_knowledge(project: Project) -> str:
    """Canonical project brief at atelier root (user-edited)."""
    if project.knowledge_path.is_file():
        return project.knowledge_path.read_text(encoding="utf-8")
    return knowledge_template(project.name)


def knowledge_for_inject(project: Project, *, limit: int = INJECT_CHAR_LIMIT) -> str:
    """Text injected into system prompt (truncated).

    Prefer root KNOWLEDGE.md; if it is thin/polluted and workspace/KNOWLEDGE.md
    is richer, inject the workspace copy (does not overwrite root).
    """
    root = read_knowledge(project).strip()
    ws_path = _workspace_knowledge_path(project)
    ws = ""
    if ws_path.is_file():
        try:
            ws = ws_path.read_text(encoding="utf-8").strip()
        except OSError:
            ws = ""
    if ws and (not root or _looks_thin_or_polluted(root)):
        # Prefer non-polluted workspace notes over thin/polluted root.
        if not _looks_polluted(ws) and (
            _looks_polluted(root) or not _looks_thin_or_polluted(ws) or len(ws) >= len(root)
        ):
            text = ws
        else:
            text = root or knowledge_template(project.name).strip()
    else:
        text = root or knowledge_template(project.name).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n…(截断；请精简 KNOWLEDGE.md)"


def sync_knowledge_from_workspace_if_empty(project: Project) -> bool:
    """If root brief is empty/thin and workspace has a richer copy, promote it.

    Returns True when root was rewritten. Snapshots via write_knowledge.
    """
    root = ""
    if project.knowledge_path.is_file():
        root = project.knowledge_path.read_text(encoding="utf-8")
    ws_path = _workspace_knowledge_path(project)
    if not ws_path.is_file():
        return False
    try:
        ws = ws_path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not ws.strip():
        return False
    if root and not _looks_thin_or_polluted(root):
        return False
    # Do not promote polluted workspace notes.
    if _looks_polluted(ws.strip()):
        return False
    if not ws.strip():
        return False
    write_knowledge(project, ws, session_id="sync-workspace")
    return True


def write_knowledge(project: Project, content: str, *, session_id: str = "system") -> None:
    project.knowledge_history_dir.mkdir(parents=True, exist_ok=True)
    if project.knowledge_path.is_file():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = project.knowledge_history_dir / f"{ts}.md"
        shutil.copy2(project.knowledge_path, dest)
    project.knowledge_path.write_text(content.rstrip() + "\n", encoding="utf-8")
    _ = session_id


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
    for p in sorted(root.rglob("*")):
        if any(part in skip for part in p.parts):
            continue
        if p.is_file():
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            out.append(rel)
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

    body = f"""# {project.name}

> 小本本草稿（扫了一眼文件夹自动填的，随便改）。

## 我想记住的
- 大概用了: {', '.join(stack)}
{f'- README 里写着: {note}' if note else '- （还没想好也可以先空着）'}

## 随手记
- 扫到大约 {len(files)} 个文件 · {time.strftime('%Y-%m-%d')}
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


# ── Optional / legacy extract (not used on the default turn path) ───────────


def extract_knowledge_heuristic(conversation: list[dict[str, Any]]) -> KnowledgeUpdate:
    """Keyword extract — kept for opt-in / tests; default host path does not call this."""
    updates: list[KnowledgeUpdateItem] = []
    signal = re.compile(
        r"(?i)(我们决定|决定使用|采用|约定|prefer|we (decided|will use)|always use|记住|"
        r"不要再|不再使用|改为|改用|改成)"
    )
    remove_sig = re.compile(r"(?i)(废弃|删除约定|不再使用|取消|remove decision)")
    for row in conversation:
        if row.get("role") not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "")
        for line in content.splitlines():
            clean = line.strip()
            if len(clean) < 10 or len(clean) > 200:
                continue
            if remove_sig.search(clean) and signal.search(clean):
                updates.append(
                    KnowledgeUpdateItem(
                        section="决策与约定",
                        type="remove",
                        old_text=clean.lstrip("-* "),
                        evidence=clean[:120],
                    )
                )
            elif signal.search(clean):
                updates.append(
                    KnowledgeUpdateItem(
                        section="决策与约定",
                        type="add",
                        new_text=clean.lstrip("-* "),
                        evidence=clean[:120],
                    )
                )
    return KnowledgeUpdate(has_update=bool(updates), updates=updates[:8])


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
