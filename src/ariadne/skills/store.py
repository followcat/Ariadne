from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AriadneError, app_error

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path
    keywords: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    references: dict[str, str] = field(default_factory=dict)

    def index_line(self) -> str:
        return f"- {self.name}: {self.description}"


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---"):
        raise ValueError("SKILL.md must start with YAML frontmatter ---")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("SKILL.md frontmatter not closed with ---")
    meta_raw = parts[1]
    body = parts[2].lstrip("\n")
    meta: dict[str, object] = {}
    current_list_key: str | None = None
    for line in meta_raw.splitlines():
        if not line.strip():
            continue
        if current_list_key and line.strip().startswith("- "):
            item = line.strip()[2:].strip().strip('"').strip("'")
            cast = meta.setdefault(current_list_key, [])
            if isinstance(cast, list):
                cast.append(item)
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            meta[key] = []
            current_list_key = key
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()] if inner else []
            meta[key] = items
        else:
            meta[key] = value.strip('"').strip("'")
    return meta, body


class SkillStore:
    def __init__(self, skills: dict[str, Skill] | None = None) -> None:
        self._skills = dict(skills or {})

    @classmethod
    def from_dir(cls, root: Path, *, strict: bool = True) -> "SkillStore":
        root = root.resolve()
        if not root.is_dir():
            if strict:
                raise AriadneError(app_error("ARIADNE_SKILL_INVALID", f"skills dir missing: {root}"))
            return cls({})
        skills: dict[str, Skill] = {}
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            skill_md = path / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                skill = cls._load_one(path)
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise AriadneError(
                        app_error("ARIADNE_SKILL_INVALID", f"{path.name}: {exc}", path=str(path))
                    ) from exc
                continue
            skills[skill.name] = skill
        return cls(skills)

    @classmethod
    def from_dirs(cls, roots: list[Path], *, strict: bool = True) -> "SkillStore":
        merged: dict[str, Skill] = {}
        for root in roots:
            part = cls.from_dir(root, strict=strict)
            merged.update(part._skills)
        return cls(merged)

    @staticmethod
    def _load_one(path: Path) -> Skill:
        text = (path / "SKILL.md").read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        name = str(meta.get("name") or path.name).strip()
        if not NAME_RE.match(name):
            raise ValueError(f"invalid skill name {name!r}")
        if name != path.name:
            raise ValueError(f"frontmatter name {name!r} != directory {path.name!r}")
        description = str(meta.get("description") or "").strip()
        if not description:
            raise ValueError("description is required")
        if len(description) > 500:
            raise ValueError("description too long")
        if len(body) > 80_000:
            raise ValueError("body too long")
        keywords = meta.get("keywords") or []
        requires = meta.get("requires_tools") or []
        if not isinstance(keywords, list):
            keywords = [str(keywords)]
        if not isinstance(requires, list):
            requires = [str(requires)]
        refs: dict[str, str] = {}
        ref_dir = path / "references"
        if ref_dir.is_dir():
            for ref in sorted(ref_dir.glob("*.md")):
                refs[ref.name] = ref.read_text(encoding="utf-8")
        return Skill(
            name=name,
            description=description,
            body=body,
            path=path,
            keywords=[str(x) for x in keywords],
            requires_tools=[str(x) for x in requires],
            references=refs,
        )

    def list(self) -> list[Skill]:
        return list(self._skills.values())

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def index_text(self, *, limit: int = 50) -> str:
        skills = self.list()[:limit]
        if not skills:
            return "(no skills installed)"
        return "\n".join(s.index_line() for s in skills)

    def search(self, query: str, *, limit: int = 5) -> list[Skill]:
        q = (query or "").strip().lower()
        if not q:
            return []
        tokens = [t for t in re.split(r"[^a-z0-9_./-]+", q) if t]
        scored: list[tuple[int, Skill]] = []
        for skill in self._skills.values():
            hay = " ".join(
                [skill.name, skill.description, " ".join(skill.keywords), skill.body[:2000]]
            ).lower()
            score = 0
            if q in hay:
                score += 10
            for tok in tokens:
                if tok in skill.name:
                    score += 5
                if tok in skill.description.lower():
                    score += 3
                if tok in " ".join(skill.keywords).lower():
                    score += 2
                if tok in skill.body.lower():
                    score += 1
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        return [s for _, s in scored[:limit]]
