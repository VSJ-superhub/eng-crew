"""Load agent prompts from skill files instead of Python string literals.

Prompts live in `.claude/skills/<name>/SKILL.md` — markdown with YAML
frontmatter, the same layout Claude Code uses for skills. Keeping them there
means a prompt can be read, diffed, and edited without touching agent code, and
the format is already what the CLI expects if these are ever surfaced to it
directly.

A note on that: Claude Code loads skills from the *working directory's* project
(or the user's home). eng-crew runs the CLI inside the target project's
worktree, so these files are not auto-loaded by the CLI — eng-crew reads them
and passes the rendered text as the prompt. Writing them into the target repo
instead would put eng-crew's internals into someone else's project and risk the
agent committing them.

Substitution is `{name}` and deliberately tolerant: an unknown placeholder is
left alone rather than raising, and braces that are not placeholders (JSON
examples, f-string-looking snippets) pass through untouched. str.format cannot
do that, which is why these templates no longer need `{{` escaping.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_PACKAGE_DIR = Path(__file__).resolve().parent          # .../eng_crew
_REPO_ROOT = _PACKAGE_DIR.parent                        # .../eng-crew (source checkout)

# Source checkouts keep skills at the repo root, where a developer expects
# .claude/ to live. Wheels carry a copy inside the package (see the
# force-include in pyproject.toml), because the repo root is not installed.
_SKILL_DIR_CANDIDATES = (
    _REPO_ROOT / ".claude" / "skills",
    _PACKAGE_DIR / ".claude" / "skills",
)


class SkillNotFound(LookupError):
    """Raised when a named skill has no SKILL.md."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: Path

    def render(self, **values: object) -> str:
        return fill(self.body, values)


def skills_dir() -> Path:
    """Where skills are read from. ENG_CREW_SKILLS_DIR overrides everything."""
    override = os.environ.get("ENG_CREW_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    for candidate in _SKILL_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    return _SKILL_DIR_CANDIDATES[0]


def fill(template: str, values: dict) -> str:
    """Replace {name} placeholders. Unknown names and stray braces are kept."""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key in values:
            return str(values[key])
        return match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")


_cache: dict[str, Skill] = {}


def load(name: str, *, refresh: bool = False) -> Skill:
    """Load a skill by directory name."""
    if not refresh and name in _cache:
        return _cache[name]

    path = skills_dir() / name / "SKILL.md"
    if not path.is_file():
        raise SkillNotFound(f"no skill {name!r} at {path}")

    meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    skill = Skill(
        name=str(meta.get("name") or name),
        description=str(meta.get("description") or ""),
        body=body.strip(),
        path=path,
    )
    _cache[name] = skill
    return skill


def render(name: str, **values: object) -> str:
    """Load a skill and fill its placeholders. The common entry point."""
    return load(name).render(**values)


def available() -> list[str]:
    """Names of every skill on disk, sorted."""
    root = skills_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "SKILL.md").is_file())


def clear_cache() -> None:
    _cache.clear()
