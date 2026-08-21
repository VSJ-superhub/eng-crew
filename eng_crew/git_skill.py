from __future__ import annotations

import re
import subprocess
import sys
import time
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


# ── Worktree isolation ──────────────────────────────────────────────────────────
#
# Without this, a run calls ensure_branch(), which stashes the developer's
# uncommitted work and switches the checkout they are sitting in onto a new
# branch. A worktree gives the run its own directory and branch, and leaves the
# main checkout — branch, working tree, and stash — untouched.

def repo_root(project_path: str | Path) -> Path:
    """Absolute path of the repository's top level."""
    root = Path(project_path).expanduser().resolve()
    return Path(_git(["rev-parse", "--show-toplevel"], cwd=root))


def default_worktree_dir(project_path: str | Path) -> Path:
    """Where worktrees live: a sibling of the repo, never inside it.

    Inside the repo they would show up as untracked files in the main checkout.
    """
    root = repo_root(project_path)
    return root.parent / ".eng-crew-worktrees" / root.name


def create_worktree(
    project_path: str | Path,
    branch: str,
    *,
    worktree_dir: str | Path | None = None,
    base: str = "HEAD",
) -> Path:
    """Create a worktree on a new branch. Returns its path."""
    root = repo_root(project_path)
    parent = Path(worktree_dir).expanduser().resolve() if worktree_dir else default_worktree_dir(root)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / branch.replace("/", "_")

    if target.exists():
        raise GitError(f"worktree path already exists: {target}")

    _git(["worktree", "add", "-b", branch, str(target), base], cwd=root)
    return target


def list_worktrees(project_path: str | Path) -> list[dict]:
    """Return [{path, branch}] for every worktree of this repo."""
    root = repo_root(project_path)
    out = _git(["worktree", "list", "--porcelain"], cwd=root)
    entries: list[dict] = []
    current: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].replace("refs/heads/", "")
    if current:
        entries.append(current)
    return entries


def remove_worktree(project_path: str | Path, worktree_path: str | Path, *, force: bool = False) -> None:
    """Remove a worktree. force discards uncommitted changes inside it."""
    root = repo_root(project_path)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(Path(worktree_path).expanduser().resolve()))
    _git(args, cwd=root)


def link_into_worktree(main_root: str | Path, worktree: str | Path, names: list[str]) -> list[str]:
    """Link build/dependency dirs from the main checkout into a worktree.

    A fresh worktree has no .venv or node_modules, so a project's tests cannot
    run there. Linking rather than copying keeps it cheap. Best-effort: a name
    that is missing, already present, or unlinkable is skipped, since a missing
    link degrades verification rather than breaking the run.

    Returns the names actually linked.
    """
    import os

    main_root = Path(main_root).expanduser().resolve()
    worktree = Path(worktree).expanduser().resolve()
    linked: list[str] = []

    for name in names:
        source = main_root / name
        dest = worktree / name
        if not source.is_dir() or dest.exists():
            continue
        try:
            os.symlink(source, dest, target_is_directory=True)
            linked.append(name)
            continue
        except (OSError, NotImplementedError):
            pass
        # Windows without developer mode refuses symlinks; junctions need no
        # elevation and behave the same for directory reads.
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dest), str(source)],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                linked.append(name)
    return linked


def worktree_status(project_path: str | Path, worktree_path: str | Path) -> dict:
    """Describe a worktree well enough to decide whether losing it is safe.

    ``dirty``    — uncommitted changes. On the single-agent tier nothing commits,
                   so these ARE the run's output. Never discard them silently.
    ``unmerged`` — commits on its branch that no other branch contains.
    ``age_days`` — age of the worktree directory.
    """
    root = repo_root(project_path)
    wt = Path(worktree_path).expanduser().resolve()
    info: dict = {"path": str(wt), "exists": wt.is_dir(), "dirty": False,
                  "unmerged": 0, "age_days": 0.0, "branch": None}
    if not info["exists"]:
        return info

    try:
        info["branch"] = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
        info["dirty"] = bool(_git(["status", "--porcelain"], cwd=wt))
        # Commits on this branch that exist on no other branch. Ask by branch
        # name, not HEAD — this runs in the main checkout, where HEAD is its own
        # branch, and would always report zero.
        branch = info["branch"]
        if branch and branch != "HEAD":
            out = _git(
                ["log", "--oneline", branch, "--not", "--exclude=" + branch, "--branches"],
                cwd=root,
            )
            info["unmerged"] = len([ln for ln in out.splitlines() if ln.strip()])
    except GitError:
        # An unreadable worktree is not a safe deletion candidate.
        info["dirty"] = True

    try:
        info["age_days"] = (time.time() - wt.stat().st_mtime) / 86400.0
    except OSError:
        pass
    return info


def prunable_worktrees(
    project_path: str | Path,
    *,
    keep_last: int = 5,
    max_age_days: float = 7.0,
) -> list[dict]:
    """Worktrees that are safe to delete, oldest first.

    Safe means: not the main worktree, no uncommitted changes, no commits that
    exist nowhere else, and older than max_age_days. The newest ``keep_last``
    are held back regardless of age so recent work stays reviewable.
    """
    root = repo_root(project_path)
    entries = []
    for wt in list_worktrees(project_path):
        path = Path(wt["path"]).resolve()
        if path == root.resolve():
            continue                      # never the checkout you work in
        entries.append(worktree_status(root, path))

    entries.sort(key=lambda e: e["age_days"])          # newest first
    candidates = entries[keep_last:]                   # hold back the newest N
    return sorted(
        [
            e for e in candidates
            if not e["dirty"] and e["unmerged"] == 0 and e["age_days"] >= max_age_days
        ],
        key=lambda e: -e["age_days"],
    )


def prune_worktrees(
    project_path: str | Path,
    *,
    keep_last: int = 5,
    max_age_days: float = 7.0,
    dry_run: bool = False,
) -> list[str]:
    """Remove worktrees that are safe to remove. Returns the paths handled."""
    root = repo_root(project_path)
    removed: list[str] = []
    for entry in prunable_worktrees(root, keep_last=keep_last, max_age_days=max_age_days):
        if dry_run:
            removed.append(entry["path"])
            continue
        try:
            remove_worktree(root, entry["path"])
            removed.append(entry["path"])
        except GitError as exc:
            print(f"[git_skill] could not remove {entry['path']}: {exc}", file=sys.stderr)

    if not dry_run:
        try:
            # Clear admin records for directories deleted outside git.
            _git(["worktree", "prune"], cwd=root)
        except GitError:
            pass
    return removed
