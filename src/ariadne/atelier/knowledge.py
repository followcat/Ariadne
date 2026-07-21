"""KNOWLEDGE.md maintenance: template, heuristic refresh, history, optional LLM."""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable

from .models import Project

KNOWLEDGE_TEMPLATE = """# {name} 项目知识

## 技术栈
- [待确认]

## 关键决策
- （初始为空）

## 约定
- （初始为空）

## 经验教训
- （初始为空）

## 进行中的工作
- （初始为空）
"""


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


def read_knowledge(project: Project) -> str:
    if project.knowledge_path.is_file():
        return project.knowledge_path.read_text(encoding="utf-8")
    return knowledge_template(project.name)


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


def heuristic_refresh(project: Project) -> str:
    """Fill 技术栈 from file tree / README without LLM."""
    files = _scan_tree(project.workspace_path)
    stack: list[str] = []
    joined = " ".join(files).lower()
    if any(f.endswith(".py") for f in files):
        stack.append("Python")
    if "pyproject.toml" in files or "requirements.txt" in files:
        stack.append("Python packaging (pyproject/requirements)")
    if any(f.endswith((".ts", ".tsx", ".js", ".jsx")) for f in files):
        stack.append("JavaScript/TypeScript")
    if "package.json" in files:
        stack.append("Node.js")
    if "Dockerfile" in files or "docker-compose.yml" in files:
        stack.append("Docker")
    if "Cargo.toml" in files:
        stack.append("Rust")
    if not stack:
        stack.append("[待确认] 未能从文件树推断技术栈")

    decisions = ["- （初始为空）"]
    readme = None
    for cand in ("README.md", "README.zh-CN.md", "readme.md"):
        p = project.workspace_path / cand
        if p.is_file():
            readme = p.read_text(encoding="utf-8", errors="replace")[:2000]
            break
    if readme:
        decisions = [f"- 见 README 摘要: {readme.splitlines()[0][:120]}"]

    body = f"""# {project.name} 项目知识

## 技术栈
{chr(10).join('- ' + s for s in stack)}

## 关键决策
{chr(10).join(decisions)}

## 约定
- （初始为空）

## 经验教训
- （初始为空）

## 进行中的工作
- （初始为空）

<!-- generated_by: heuristic refresh {time.strftime('%Y-%m-%d')} -->
"""
    if files:
        body += "\n## 文件树摘录\n" + "\n".join(f"- `{f}`" for f in files[:40]) + "\n"
    return body


def apply_updates(content: str, update: KnowledgeUpdate) -> str:
    """Apply section line adds (P0: add-only under matching ## heading)."""
    if not update.has_update or not update.updates:
        return content
    text = content
    for item in update.updates:
        if item.type != "add" or not item.new_text.strip():
            continue
        section = item.section.strip()
        heading = f"## {section}"
        # find heading
        pattern = re.compile(rf"(?m)^##\s+{re.escape(section)}\s*$")
        m = pattern.search(text)
        line = item.new_text.strip()
        if not line.startswith("-"):
            line = "- " + line
        if not m:
            text = text.rstrip() + f"\n\n## {section}\n{line}\n"
            continue
        # insert after heading line
        insert_at = m.end()
        text = text[:insert_at] + "\n" + line + text[insert_at:]
    return text


def extract_knowledge_heuristic(conversation: list[dict[str, Any]]) -> KnowledgeUpdate:
    """Keyword-based extraction without LLM (conservative)."""
    updates: list[KnowledgeUpdateItem] = []
    signal = re.compile(
        r"(?i)(我们决定|决定使用|采用|约定|prefer|we (decided|will use)|always use|记住)"
    )
    for row in conversation:
        if row.get("role") not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "")
        for line in content.splitlines():
            clean = line.strip()
            if len(clean) < 10 or len(clean) > 200:
                continue
            if signal.search(clean):
                updates.append(
                    KnowledgeUpdateItem(
                        section="关键决策",
                        type="add",
                        new_text=clean.lstrip("-* "),
                        evidence=clean[:120],
                    )
                )
    return KnowledgeUpdate(has_update=bool(updates), updates=updates[:5])


async def extract_knowledge_llm(
    conversation: list[dict[str, Any]],
    current_knowledge: str,
    *,
    complete: Callable[[str], Awaitable[str]] | None = None,
) -> KnowledgeUpdate:
    """Optional LLM path; falls back to heuristic if complete is None."""
    if complete is None:
        return extract_knowledge_heuristic(conversation)
    # Minimal structured request — implementation may improve later
    prompt = (
        "Extract project knowledge updates as lines for section 关键决策 only. "
        "If none, reply NONE.\n\nKnowledge:\n"
        + current_knowledge[:3000]
        + "\n\nDialogue:\n"
        + "\n".join(f"{r.get('role')}: {r.get('content')}" for r in conversation[-6:])
    )
    try:
        raw = await complete(prompt)
    except Exception:
        return extract_knowledge_heuristic(conversation)
    if not raw or raw.strip().upper() == "NONE":
        return KnowledgeUpdate(has_update=False)
    updates = [
        KnowledgeUpdateItem(section="关键决策", type="add", new_text=line.strip().lstrip("- "))
        for line in raw.splitlines()
        if line.strip() and line.strip().upper() != "NONE"
    ][:5]
    return KnowledgeUpdate(has_update=bool(updates), updates=updates)


def generate_branch_summary(transcript: list[dict[str, Any]], *, branch_name: str) -> str:
    """Heuristic branch summary (no LLM required)."""
    users = [str(r.get("content") or "")[:200] for r in transcript if r.get("role") == "user"]
    asst = [str(r.get("content") or "")[:200] for r in transcript if r.get("role") == "assistant"]
    lines = [
        f"## 工作摘要（分支 `{branch_name}`）",
        f"- 用户轮次: {len(users)}",
        f"- 助手轮次: {len(asst)}",
    ]
    if users:
        lines.append(f"- 首条用户意图: {users[0][:160]}")
    if asst:
        lines.append(f"- 末条助手摘要: {asst[-1][:160]}")
    lines.append("- 结论: [待主会话确认]")
    return "\n".join(lines) + "\n"
