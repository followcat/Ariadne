from __future__ import annotations

import difflib
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import AriadneError, app_error
from ..memory.json_file import locked_read_json, locked_update_json, locked_write_json

if TYPE_CHECKING:
    from .store import SkillStore


def render_skill_text(
    *,
    name: str,
    description: str,
    body: str,
    keywords: list[str],
    version: str,
) -> str:
    lines = [
        "---",
        f"name: {name}",
        f"description: {description.strip()}",
        f"keywords: [{', '.join(keywords)}]" if keywords else "keywords: []",
        f'version: "{version}"',
        "---",
        "",
        body.lstrip(),
    ]
    return "\n".join(lines).rstrip() + "\n"


@dataclass(slots=True)
class SkillPatchStore:
    skill_store: SkillStore
    path: Path
    max_proposals: int = 256

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            locked_write_json(self.path, {"schema_version": 1, "proposals": []})

    def _read(self) -> dict[str, Any]:
        data = locked_read_json(
            self.path, default={"schema_version": 1, "proposals": []}
        )
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise AriadneError(
                app_error("ARIADNE_SKILL_PATCH_INVALID", "unknown skill patch schema")
            )
        return data

    def list(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._read().get("proposals") or [])
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        return rows

    def get(self, proposal_id: str) -> dict[str, Any]:
        for row in self.list():
            if row.get("proposal_id") == proposal_id:
                return row
        raise AriadneError(
            app_error(
                "ARIADNE_SKILL_PATCH_NOT_FOUND",
                f"skill patch proposal not found: {proposal_id}",
            )
        )

    def propose(
        self,
        *,
        name: str,
        description: str,
        body: str,
        keywords: list[str],
        evidence: list[str],
        expected_version: str,
    ) -> dict[str, Any]:
        skill = self.skill_store.get(name)
        if skill is None:
            raise AriadneError(
                app_error("ARIADNE_SKILL_NOT_FOUND", f"skill not found: {name}")
            )
        if skill.namespace != "user":
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_INVALID",
                    "only user skills may be patched",
                    name=name,
                    namespace=skill.namespace,
                )
            )
        if skill.version != expected_version:
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_PATCH_CONFLICT",
                    "skill version changed before proposal",
                    name=name,
                    expected_version=expected_version,
                    current_version=skill.version,
                )
            )
        evidence = [str(item).strip() for item in evidence if str(item).strip()]
        if not evidence:
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_PATCH_INVALID",
                    "a skill patch proposal requires outcome/user evidence",
                )
            )
        if not description.strip() or not body.strip():
            raise AriadneError(
                app_error("ARIADNE_SKILL_PATCH_INVALID", "description and body are required")
            )
        next_version = self.skill_store.bump_version(skill.version)
        proposed_text = render_skill_text(
            name=name,
            description=description,
            body=body,
            keywords=keywords,
            version=next_version,
        )
        current_text = (skill.path / "SKILL.md").read_text(encoding="utf-8")
        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                proposed_text.splitlines(keepends=True),
                fromfile=f"{name}/SKILL.md@{skill.version}",
                tofile=f"{name}/SKILL.md@{next_version}",
            )
        )
        if not diff:
            raise AriadneError(
                app_error("ARIADNE_SKILL_PATCH_INVALID", "proposal makes no change")
            )
        now = time.time()
        row = {
            "proposal_id": uuid.uuid4().hex[:16],
            "name": name,
            "expected_version": expected_version,
            "proposed_version": next_version,
            "description": description.strip(),
            "body": body,
            "keywords": list(keywords),
            "evidence": evidence,
            "diff": diff,
            "proposed_text": proposed_text,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            proposals = data.setdefault("proposals", [])
            if len(proposals) >= self.max_proposals:
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_CAPACITY",
                        "skill patch proposal capacity exceeded",
                    )
                )
            if any(
                item.get("name") == name and item.get("status") == "pending"
                for item in proposals
            ):
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_CONFLICT",
                        "the skill already has a pending patch proposal",
                        name=name,
                    )
                )
            proposals.append(row)
            return data

        locked_update_json(
            self.path, mut, default={"schema_version": 1, "proposals": []}
        )
        return {key: value for key, value in row.items() if key != "proposed_text"}

    def confirm(self, *, proposal_id: str, confirmed_by: str) -> dict[str, Any]:
        actor = confirmed_by.strip()
        if not actor:
            raise AriadneError(
                app_error("ARIADNE_SKILL_CONFIRMATION_REQUIRED", "confirmed_by is required")
            )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            proposal = next(
                (
                    row
                    for row in data.get("proposals") or []
                    if row.get("proposal_id") == proposal_id
                ),
                None,
            )
            if proposal is None:
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_NOT_FOUND",
                        f"skill patch proposal not found: {proposal_id}",
                    )
                )
            if proposal.get("status") != "pending":
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_CONFLICT",
                        "only pending proposals can be confirmed",
                        proposal_id=proposal_id,
                        status=proposal.get("status"),
                    )
                )
            name = str(proposal["name"])
            skill = self.skill_store.get(name)
            if skill is None or skill.version != proposal.get("expected_version"):
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_CONFLICT",
                        "skill changed after proposal; create a new diff",
                        name=name,
                        expected_version=proposal.get("expected_version"),
                        current_version=skill.version if skill else None,
                    )
                )
            versions = self.skill_store.user_root / ".versions" / name
            versions.mkdir(parents=True, exist_ok=True)
            snapshot = versions / proposal_id
            if snapshot.exists():
                raise AriadneError(
                    app_error(
                        "ARIADNE_SKILL_PATCH_CONFLICT",
                        "proposal snapshot already exists",
                        proposal_id=proposal_id,
                    )
                )
            shutil.copytree(skill.path, snapshot)
            target = skill.path / "SKILL.md"
            temporary = skill.path / f".SKILL.{proposal_id}.tmp"
            temporary.write_text(str(proposal["proposed_text"]), encoding="utf-8")
            os.replace(temporary, target)
            updated = self.skill_store._load_one(skill.path, namespace="user")
            self.skill_store._skills[name] = updated
            proposal["status"] = "applied"
            proposal["confirmed_by"] = actor
            proposal["updated_at"] = time.time()
            proposal["snapshot"] = str(snapshot)
            result.update(
                {
                    "proposal_id": proposal_id,
                    "status": "applied",
                    "name": name,
                    "version": updated.version,
                    "previous_version": proposal["expected_version"],
                    "confirmed_by": actor,
                    "snapshot": str(snapshot),
                }
            )
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "proposals": []})
        return result

    def reject(self, *, proposal_id: str, rejected_by: str, reason: str) -> dict[str, Any]:
        if not rejected_by.strip() or not reason.strip():
            raise AriadneError(
                app_error(
                    "ARIADNE_SKILL_CONFIRMATION_REQUIRED",
                    "rejected_by and reason are required",
                )
            )
        result: dict[str, Any] = {}

        def mut(data: dict[str, Any]) -> dict[str, Any]:
            proposal = next(
                (row for row in data.get("proposals") or [] if row.get("proposal_id") == proposal_id),
                None,
            )
            if proposal is None:
                raise AriadneError(
                    app_error("ARIADNE_SKILL_PATCH_NOT_FOUND", "proposal not found")
                )
            if proposal.get("status") != "pending":
                raise AriadneError(
                    app_error("ARIADNE_SKILL_PATCH_CONFLICT", "proposal is not pending")
                )
            proposal.update(
                {
                    "status": "rejected",
                    "rejected_by": rejected_by.strip(),
                    "rejection_reason": reason.strip(),
                    "updated_at": time.time(),
                }
            )
            result.update(
                {"proposal_id": proposal_id, "status": "rejected", "name": proposal["name"]}
            )
            return data

        locked_update_json(self.path, mut, default={"schema_version": 1, "proposals": []})
        return result
