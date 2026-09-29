from __future__ import annotations

import logging
import os
import re
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


log = logging.getLogger(__name__)


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


def create_branch(project_path: str | Path, branch_name: str, base: str | None = None) -> str:
    """Create and checkout a new branch. Returns the branch name.

    ``base`` is the start point — another branch, a tag, a SHA. Omitted, the
    branch starts at whatever the checkout currently has checked out.
    """
    root = Path(project_path).expanduser().resolve()
    args = ["checkout", "-b", branch_name]
    if base:
        args.append(base)
    _git(args, cwd=root)
    return branch_name


def branch_exists(project_path: str | Path, ref: str) -> bool:
    """True if ``ref`` resolves to a commit in this repo (branch, tag, or SHA)."""
    root = Path(project_path).expanduser().resolve()
    try:
        _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root)
        return True
    except (GitError, FileNotFoundError):
        return False


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
    base: str | None = None,
) -> str:
    """Create a timestamped branch in the target project.

    Stashes dirty working tree changes if stash_changes is True. ``base`` bases
    the new branch on another branch instead of the checkout's current HEAD.
    Returns the new branch name.
    """
    root = Path(project_path).expanduser().resolve()
    if not is_working_tree_clean(root) and stash_changes:
        stash(root)
    branch = make_branch_name(prefix, slug)
    create_branch(root, branch, base)
    return branch


_LINK_MODE = "120000"    # symlink blob
_GITLINK_MODE = "160000"  # embedded repository


def _registered_submodules(root: Path) -> set[str]:
    """Paths git legitimately tracks as submodules."""
    gitmodules = root / ".gitmodules"
    if not gitmodules.exists():
        return set()
    try:
        out = _git(
            ["config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
            cwd=root,
        )
    except GitError:
        return set()
    return {line.split(maxsplit=1)[1] for line in out.splitlines() if " " in line}


def _unstage_dependency_links(root: Path) -> list[str]:
    """Drop staged dependency links from the index. Returns the paths dropped.

    link_into_worktree symlinks .venv and node_modules into a worktree so the
    project's tests can run there. A .gitignore entry like "node_modules/" has a
    trailing slash and so matches a directory, never the symlink standing in for
    one, which leaves `add -A` free to stage the link. Committing it writes the
    main checkout's absolute path into the tree — noise at best, and on another
    machine a path that does not exist.

    Only links to directories and unregistered embedded repos are dropped; a
    symlink to a file is a normal thing to track.
    """
    dropped: list[str] = []
    submodules = _registered_submodules(root)
    for line in _git(["ls-files", "--stage"], cwd=root).splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        mode = meta.split()[0] if meta.split() else ""
        if mode == _GITLINK_MODE:
            if path in submodules:
                continue
        elif mode == _LINK_MODE:
            # is_dir() follows the link, so it is True for both a POSIX symlink
            # and a Windows junction pointing at a directory.
            if not (root / path).is_dir():
                continue
        else:
            continue
        # Resets the index entry to HEAD, or removes it when it is newly added,
        # without touching what is on disk.
        _git(["reset", "-q", "--", path], cwd=root)
        dropped.append(path)
    return dropped


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
        dropped = _unstage_dependency_links(root)
        if dropped:
            log.info("commit_all: left dependency links unstaged: %s", ", ".join(dropped))
    if not _git(["diff", "--cached", "--name-only"], cwd=root):
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


def is_directory_link(path: str | Path) -> bool:
    """True for a link that stands in for a directory.

    Covers a POSIX symlink and a Windows junction alike — is_symlink() reports
    False for a junction, which is what link_into_worktree falls back to when
    Windows refuses symlinks, so asking that alone misses them.
    """
    entry = Path(path)
    try:
        if entry.is_symlink():
            return entry.is_dir()
        if os.name == "nt":
            attrs = getattr(os.lstat(entry), "st_file_attributes", 0)
            return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT) and entry.is_dir()
    except OSError:
        return False
    return False


def unlink_dependency_links(worktree: str | Path) -> list[str]:
    """Drop the links link_into_worktree left in a worktree.

    Removing the worktree directory tree with these still in place risks
    following a junction into the main checkout's .venv and deleting the real
    thing. Unlinking first removes the link, never its target. Returns the names
    unlinked.
    """
    wt = Path(worktree).expanduser().resolve()
    unlinked: list[str] = []
    if not wt.is_dir():
        return unlinked
    for entry in wt.iterdir():
        if not is_directory_link(entry):
            continue
        try:
            os.rmdir(entry)         # junction, or a dir symlink on Windows
        except OSError:
            try:
                entry.unlink()      # POSIX symlink
            except OSError:
                continue
        unlinked.append(entry.name)
    return unlinked


def remove_worktree(project_path: str | Path, worktree_path: str | Path, *, force: bool = False) -> None:
    """Remove a worktree. force discards uncommitted changes inside it."""
    root = repo_root(project_path)
    target = Path(worktree_path).expanduser().resolve()
    unlink_dependency_links(target)
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    _git(args, cwd=root)


def link_into_worktree(main_root: str | Path, worktree: str | Path, names: list[str]) -> list[str]:
    """Link build/dependency dirs from the main checkout into a worktree.

    A fresh worktree has no .venv or node_modules, so a project's tests cannot
    run there. Linking rather than copying keeps it cheap. Best-effort: a name
    that is missing, already present, or unlinkable is skipped, since a missing
    link degrades verification rather than breaking the run.

    Returns the names actually linked.
    """
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


def _has_real_changes(worktree: Path, porcelain: str) -> bool:
    """Whether a worktree's status is the run's work rather than its plumbing.

    link_into_worktree drops a .venv/node_modules link into every worktree, and
    a .gitignore entry like ".venv/" matches a directory, never the link
    standing in for one — so git reports it as untracked. Counting that as
    uncommitted work marked every worktree dirty, which held all of them back
    from pruning forever.
    """
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("?? "):
            rel = line[3:].strip().strip('"')
            if is_directory_link(worktree / rel):
                continue
        return True
    return False


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
        info["dirty"] = _has_real_changes(wt, _git(["status", "--porcelain"], cwd=wt))
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
    exclude: list[str | Path] | None = None,
) -> list[dict]:
    """Worktrees that are safe to delete, oldest first.

    Safe means: not the main worktree, no uncommitted changes, no commits that
    exist nowhere else, and older than max_age_days. The newest ``keep_last``
    are held back regardless of age so recent work stays reviewable. ``exclude``
    spares specific paths — a caller pruning while a run holds a worktree open
    passes that worktree here.
    """
    root = repo_root(project_path)
    spared = {Path(p).expanduser().resolve() for p in (exclude or [])}
    entries = []
    for wt in list_worktrees(project_path):
        path = Path(wt["path"]).resolve()
        if path == root.resolve():
            continue                      # never the checkout you work in
        if path in spared:
            continue
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
    exclude: list[str | Path] | None = None,
) -> list[str]:
    """Remove worktrees that are safe to remove. Returns the paths handled."""
    root = repo_root(project_path)
    removed: list[str] = []
    for entry in prunable_worktrees(
        root, keep_last=keep_last, max_age_days=max_age_days, exclude=exclude
    ):
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


def prune_spent_worktrees(
    project_path: str | Path,
    *,
    exclude: list[str | Path] | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Remove every worktree that is clean and fully merged, whatever its age.

    Run this when a run finishes. Age is the wrong guard at that moment: a
    worktree whose branch holds nothing another branch does not already hold has
    no output left to review, so keeping it for a week only makes them pile up.
    The safety checks are the same ones prune_worktrees uses — anything dirty or
    unmerged (which is what a run's output looks like) is still kept.
    """
    return prune_worktrees(
        project_path,
        keep_last=0,
        max_age_days=0.0,
        dry_run=dry_run,
        exclude=exclude,
    )


def branch_diff(
    project_path: str | Path,
    branch: str,
    base: str = "HEAD",
    *,
    max_chars: int = 0,
) -> str:
    """The diff a branch adds on top of ``base`` — what a run produced.

    Three dots, not two: the comparison is against the merge base, so commits
    that landed on ``base`` after the run forked do not show up as the run's
    work. ``max_chars`` truncates, for a reader with a context window.
    """
    root = repo_root(project_path)
    text = _git(["diff", f"{base}...{branch}"], cwd=root)
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + "\n... [diff truncated]"
    return text


def changed_paths(project_path: str | Path, base: str = "HEAD") -> list[str]:
    """Repo-relative paths this working tree changed since ``base``.

    Covers tracked modifications and untracked files alike — the execution
    tiers leave their output uncommitted, so a brand new test file shows up
    only as untracked. Deleted paths are omitted: there is nothing to replay.
    """
    root = repo_root(project_path)
    paths: set[str] = set()

    for line in _git(["diff", "--name-only", "--diff-filter=d", base], cwd=root).splitlines():
        if line.strip():
            paths.add(line.strip())

    # -uall lists files inside untracked directories, not just the directory.
    for line in _git(["status", "--porcelain", "-uall"], cwd=root).splitlines():
        if line.startswith("?? "):
            paths.add(line[3:].strip().strip('"'))

    return sorted(p for p in paths if (root / p).is_file())


def create_detached_worktree(
    project_path: str | Path,
    base: str = "HEAD",
    *,
    worktree_dir: str | Path | None = None,
    name: str = "redphase",
) -> Path:
    """Add a throwaway detached worktree at ``base``. Returns its path.

    Detached rather than on a branch: this is a scratch checkout used to
    replay tests against pre-change code, and it must not leave a branch
    behind when it is torn down.
    """
    root = repo_root(project_path)
    parent = Path(worktree_dir).expanduser().resolve() if worktree_dir else default_worktree_dir(root)
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{name}-{int(time.time() * 1000)}"

    _git(["worktree", "add", "--detach", str(target), base], cwd=root)
    return target
