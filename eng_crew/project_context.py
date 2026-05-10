from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_MAX_FILE_BYTES = 64 * 1024  # 64 KB cap per file


@dataclass
class ProjectContext:
    project_path: Path
    claude_md: str
    architecture_md: str
    readme_md: str
    memory_md: str
    extra: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        """Return a single prompt-ready string with all context sections."""
        parts: list[str] = []
        if self.claude_md:
            parts.append(f"# CLAUDE.md\n{self.claude_md}")
        if self.architecture_md:
            parts.append(f"# ARCHITECTURE.md\n{self.architecture_md}")
        if self.readme_md:
            parts.append(f"# README.md\n{self.readme_md}")
        if self.memory_md:
            parts.append(f"# MEMORY.md\n{self.memory_md}")
        for name, content in self.extra.items():
            parts.append(f"# {name}\n{content}")
        return "\n\n---\n\n".join(parts)

    def __bool__(self) -> bool:
        return bool(self.claude_md or self.architecture_md or self.readme_md)


def _read_file(path: Path, max_bytes: int = _MAX_FILE_BYTES) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except OSError:
        return ""


def load_project_context(
    project_path: str | Path,
    extra_files: Optional[list[str]] = None,
) -> ProjectContext:
    """Load CLAUDE.md, ARCHITECTURE.md, README.md, and MEMORY.md from project_path."""
    root = Path(project_path).expanduser().resolve()
    extra: dict[str, str] = {}
    if extra_files:
        for name in extra_files:
            content = _read_file(root / name)
            if content:
                extra[name] = content
    return ProjectContext(
        project_path=root,
        claude_md=_read_file(root / "CLAUDE.md"),
        architecture_md=_read_file(root / "ARCHITECTURE.md"),
        readme_md=_read_file(root / "README.md"),
        memory_md=_read_file(root / ".eng-crew" / "MEMORY.md"),
        extra=extra,
    )
