from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import AriadneError, app_error
from ..memory.embeddings import EmbeddingProvider, HashEmbeddingProvider, cosine

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
    version: str = "1"
    namespace: str = "builtin"

    def index_line(self) -> str:
        return f"- {self.name}: {self.description}"

    def searchable_text(self) -> str:
        return " ".join([self.name, self.description, " ".join(self.keywords), self.body[:2000]])


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
    def __init__(
        self,
        skills: dict[str, Skill] | None = None,
        *,
        user_root: Path | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._skills = dict(skills or {})
        self.user_root = user_root
        self.embedder = embedder or HashEmbeddingProvider(dims=32)
        self._emb_cache: dict[str, list[float]] = {}

    @classmethod
    def from_dir(
        cls,
        root: Path,
        *,
        strict: bool = True,
        user_root: Path | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> "SkillStore":
        root = root.resolve()
        if not root.is_dir():
            if strict:
                raise AriadneError(app_error("ARIADNE_SKILL_INVALID", f"skills dir missing: {root}"))
            return cls({}, user_root=user_root, embedder=embedder)
        skills: dict[str, Skill] = {}
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            if not (path / "SKILL.md").is_file():
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
        return cls(skills, user_root=user_root, embedder=embedder)

    @classmethod
    def from_dirs(
        cls,
        roots: list[Path],
        *,
        strict: bool = True,
        user_root: Path | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> "SkillStore":
        merged: dict[str, Skill] = {}
        for root in roots:
            part = cls.from_dir(root, strict=strict, user_root=user_root, embedder=embedder)
            merged.update(part._skills)
        return cls(merged, user_root=user_root, embedder=embedder)

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
        ns = "user" if "user" in path.parts[-3:] else "builtin"
        return Skill(
            name=name,
            description=description,
            body=body,
            path=path,
            keywords=[str(x) for x in keywords],
            requires_tools=[str(x) for x in requires],
            references=refs,
            version=str(meta.get("version") or "1"),
            namespace=ns,
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
            hay = skill.searchable_text().lower()
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

    async def search_hybrid(self, query: str, *, limit: int = 5) -> list[Skill]:
        lexical = self.search(query, limit=max(limit * 3, 10))
        if not lexical:
            return []
        q_emb = (await self.embedder.embed([query]))[0]
        scored: list[tuple[float, Skill]] = []
        for i, skill in enumerate(lexical):
            key = skill.name + ":" + skill.version
            if key not in self._emb_cache:
                emb = (await self.embedder.embed([skill.searchable_text()]))[0]
                self._emb_cache[key] = emb
            emb_score = cosine(q_emb, self._emb_cache[key])
            # blend rank position with embedding
            lex_score = 1.0 / (1 + i)
            scored.append((0.4 * lex_score + 0.6 * emb_score, skill))
        scored.sort(key=lambda item: -item[0])
        return [s for _, s in scored[:limit]]

    def manage(
        self,
        *,
        action: str,
        name: str,
        description: str = "",
        body: str = "",
        keywords: list[str] | None = None,
    ) -> dict[str, object]:
        """Create/update/delete user skills with versioned directories."""
        if self.user_root is None:
            raise AriadneError(app_error("ARIADNE_CONFIG_INVALID", "user skills root not configured"))
        action = (action or "").strip().lower()
        name = (name or "").strip()
        if not NAME_RE.match(name):
            raise AriadneError(app_error("ARIADNE_SKILL_INVALID", f"invalid skill name {name!r}"))
        self.user_root.mkdir(parents=True, exist_ok=True)
        skill_dir = self.user_root / name
        if action == "delete":
            if not skill_dir.exists():
                raise AriadneError(app_error("ARIADNE_SKILL_NOT_FOUND", f"skill not found: {name}"))
            # version snapshot then remove active
            versions = self.user_root / ".versions" / name
            versions.mkdir(parents=True, exist_ok=True)
            stamp = str(int(__import__("time").time()))
            target = versions / stamp
            if skill_dir.exists():
                import shutil

                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(skill_dir, target)
                shutil.rmtree(skill_dir)
            self._skills.pop(name, None)
            return {"action": "delete", "name": name, "versioned_to": str(target)}
        if action not in {"create", "update"}:
            raise AriadneError(app_error("ARIADNE_INVALID_TOOL_ARGS", "action must be create|update|delete"))
        if not description.strip():
            raise AriadneError(app_error("ARIADNE_SKILL_INVALID", "description required"))
        if not body.strip():
            raise AriadneError(app_error("ARIADNE_SKILL_INVALID", "body required"))
        if action == "create" and skill_dir.exists():
            raise AriadneError(app_error("ARIADNE_SKILL_INVALID", f"skill already exists: {name}"))
        if action == "update" and skill_dir.exists():
            import shutil

            versions = self.user_root / ".versions" / name
            versions.mkdir(parents=True, exist_ok=True)
            stamp = str(int(__import__("time").time()))
            shutil.copytree(skill_dir, versions / stamp, dirs_exist_ok=True)
        skill_dir.mkdir(parents=True, exist_ok=True)
        kw = keywords or []
        fm = [
            "---",
            f"name: {name}",
            f"description: {description.strip()}",
            f"keywords: [{', '.join(kw)}]" if kw else "keywords: []",
            "version: \"1\"",
            "---",
            "",
            body.lstrip() + ("\n" if not body.endswith("\n") else ""),
        ]
        (skill_dir / "SKILL.md").write_text("\n".join(fm), encoding="utf-8")
        skill = self._load_one(skill_dir)
        skill.namespace = "user"
        self._skills[name] = skill
        return {"action": action, "name": name, "path": str(skill_dir), "description": skill.description}
