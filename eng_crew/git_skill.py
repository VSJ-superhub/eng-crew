from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional


class GitError(Exception):
    """Raised when a git command fails in the target project."""


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command in cwd and return stdout. Raises GitError on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def is_git_repo(project_path: str | Path) -> bool:
    root = Path(project_path).expanduser().resolve()
    try:
        _git(["rev-parse", "--git-dir"], cwd=root)
        return True
    except (GitError, FileNotFoundError):
        return False


def current_branch(project_path: str | Path) -> str:
    root = Path(project_path).expanduser().resolve()
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)


def make_branch_name(prefix: str, slug: str) -> str:
    """Return a timestamped branch name like prefix/20260508-153000-some-slug."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")[:48]
    return f"{prefix}/{ts}-{safe_slug}"


def create_branch(project_path: str | Path, branch_name: str) -> str:
    """Create and checkout a new branch. Returns the branch name."""
    root = Path(project_path).expanduser().resolve()
    _git(["checkout", "-b", branch_name], cwd=root)
    return branch_name


def checkout_branch(project_path: str | Path, branch_name: str) -> None:
    """Checkout an existing branch."""
    root = Path(project_path).expanduser().resolve()
    _git(["checkout", branch_name], cwd=root)


def is_working_tree_clean(project_path: str | Path) -> bool:
    root = Path(project_path).expanduser().resolve()
    try:
        return not bool(_git(["status", "--porcelain"], cwd=root))
    except GitError:
        return False


def stash(project_path: str | Path, message: str = "eng-crew auto-stash") -> bool:
    """Stash uncommitted changes. Returns True if anything was stashed."""
    root = Path(project_path).expanduser().resolve()
    try:
        out = _git(["stash", "push", "-m", message], cwd=root)
        return "No local changes" not in out
    except GitError:
        return False


def stash_pop(project_path: str | Path) -> None:
    root = Path(project_path).expanduser().resolve()
    _git(["stash", "pop"], cwd=root)


def ensure_branch(
    project_path: str | Path,
    prefix: str,
    slug: str,
    *,
    stash_changes: bool = True,
) -> str:
    """Create a timestamped branch in the target project.

    Stashes dirty working tree changes if stash_changes is True.
    Returns the new branch name.
    """
    root = Path(project_path).expanduser().resolve()
    if not is_working_tree_clean(root) and stash_changes:
        stash(root)
    branch = make_branch_name(prefix, slug)
    create_branch(root, branch)
    return branch


def commit_all(
    project_path: str | Path,
    message: str,
    *,
    add_all: bool = True,
) -> Optional[str]:
    """Stage all changes and commit. Returns the commit SHA or None if nothing to commit."""
    root = Path(project_path).expanduser().resolve()
    if add_all:
        _git(["add", "-A"], cwd=root)
    if not _git(["status", "--porcelain"], cwd=root):
        return None
    _git(["commit", "-m", message], cwd=root)
    return _git(["rev-parse", "HEAD"], cwd=root)
