"""Skills: reusable procedures Bagley follows for recurring tasks.

A skill is a folder with a ``SKILL.md`` file (the agentskills.io format: YAML-style front
matter with ``name`` and ``description``, then Markdown instructions). Built-in skills ship with
the package; the user's own and learned skills live in ``<data dir>/skills`` and override a
built-in skill of the same name. Skills written for other agents can be dropped in as they are.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILTIN_DIR = Path(__file__).parent / "builtin_skills"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 12_000
MAX_DESCRIPTION = 300


class SkillError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    source: str  # builtin | learned | user
    updated: float

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "updated": self.updated,
            "editable": self.source != "builtin",
        }

    def text(self) -> str:
        return render(self.name, self.description, self.body, self.source)


def slug(name: str) -> str:
    """Turn a title like "Research a question" into ``research-a-question``."""
    value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:64].strip("-")
    if not NAME.match(value):
        raise SkillError("A skill name needs letters or digits, e.g. 'weekly-report'.")
    return value


def parse(text: str) -> tuple[dict[str, str], str]:
    """Split ``SKILL.md`` into front matter (simple ``key: value`` lines) and body."""
    meta: dict[str, str] = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return meta, text.strip()
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[m.end() :].strip()


def render(name: str, description: str, body: str, source: str = "user") -> str:
    description = " ".join(description.split())
    lines = ["---", f"name: {name}", f"description: {description}"]
    if source == "learned":
        lines.append("source: learned")
    return "\n".join([*lines, "---", "", body.strip(), ""])


class SkillStore:
    def __init__(self, root: Path, builtin: Path | None = BUILTIN_DIR) -> None:
        self.root = root
        self.builtin = builtin

    def _load(self, folder: Path, default_source: str) -> Skill | None:
        path = folder / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        meta, body = parse(text)
        name = meta.get("name") or folder.name
        if not NAME.match(name):
            name = folder.name
        source = default_source
        if default_source != "builtin":
            source = "learned" if meta.get("source") == "learned" else "user"
        return Skill(
            name=name,
            description=(meta.get("description") or "")[:MAX_DESCRIPTION],
            body=body[:MAX_BODY],
            path=path,
            source=source,
            updated=path.stat().st_mtime,
        )

    def all(self) -> list[Skill]:
        found: dict[str, Skill] = {}
        for base, source in ((self.builtin, "builtin"), (self.root, "user")):
            if base is None or not base.is_dir():
                continue
            for folder in sorted(base.iterdir()):
                if folder.is_dir() and not folder.name.startswith("."):
                    skill = self._load(folder, source)
                    if skill:
                        found[skill.name] = skill  # The user's copy wins over a built-in.
        return sorted(found.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        key = name.strip().lower()
        return next((s for s in self.all() if s.name == key), None)

    def save(self, name: str, description: str, body: str, *, source: str = "user") -> Skill:
        key = slug(name)
        description = " ".join(description.split())
        if not description:
            raise SkillError("Describe when the skill applies, in one sentence.")
        if len(description) > MAX_DESCRIPTION:
            raise SkillError(f"Keep the description under {MAX_DESCRIPTION} characters.")
        if not body.strip():
            raise SkillError("The skill needs instructions.")
        if len(body) > MAX_BODY:
            raise SkillError(f"Keep the instructions under {MAX_BODY:,} characters.")
        folder = self.root / key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "SKILL.md"
        path.write_text(render(key, description, body, source), encoding="utf-8")
        skill = self._load(folder, "user")
        assert skill
        return skill

    def delete(self, name: str) -> bool:
        """Delete the user's copy. Built-in skills can't be deleted, only overridden."""
        skill = self.get(name)
        if skill is None or skill.source == "builtin":
            return False
        shutil.rmtree(skill.path.parent, ignore_errors=True)
        return True

    def index(self, limit: int = 40) -> list[dict[str, Any]]:
        """Newest first, for the system prompt."""
        skills = sorted(self.all(), key=lambda s: (s.source == "builtin", -s.updated))
        return [s.summary() for s in skills[:limit]]

    def touch(self, name: str) -> None:
        """Mark a skill as recently used so it stays near the top of the index."""
        skill = self.get(name)
        if skill and skill.source != "builtin":
            now = time.time()
            with contextlib.suppress(OSError):
                os.utime(skill.path, (now, now))
