"""Worktree pruning must never destroy a run's output.

The single-agent tier edits files and does not commit, so a worktree's
uncommitted changes ARE the result of that run. Deleting one to tidy up would
silently throw away the work the user asked for. These tests pin the refusals
as hard as the removals.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest

from eng_crew import git_skill


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def _age(path, days: float) -> None:
    """Backdate a worktree so retention rules apply to it."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


# --- refusals -----------------------------------------------------------


def test_worktree_with_uncommitted_changes_is_never_pruned(repo):
    wt = git_skill.create_worktree(repo, "ai-team/dirty")
    (wt / "app.py").write_text("agent's unsaved work\n", encoding="utf-8")
    _age(wt, 999)

    assert git_skill.worktree_status(repo, wt)["dirty"] is True
    assert git_skill.prunable_worktrees(repo, keep_last=0, max_age_days=0) == []
    assert git_skill.prune_worktrees(repo, keep_last=0, max_age_days=0) == []
    assert wt.is_dir(), "a worktree holding uncommitted work was deleted"


def test_worktree_with_unmerged_commits_is_never_pruned(repo):
    wt = git_skill.create_worktree(repo, "ai-team/unmerged")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "agent work", cwd=wt)
    _age(wt, 999)

    assert git_skill.worktree_status(repo, wt)["unmerged"] >= 1
    assert git_skill.prune_worktrees(repo, keep_last=0, max_age_days=0) == []
    assert wt.is_dir()


def test_the_main_checkout_is_never_a_candidate(repo):
    git_skill.create_worktree(repo, "ai-team/other")
    paths = [e["path"] for e in git_skill.prunable_worktrees(repo, keep_last=0, max_age_days=0)]
    assert str(repo.resolve()) not in paths


def test_recent_worktrees_are_kept_even_when_clean(repo):
    wt = git_skill.create_worktree(repo, "ai-team/fresh")
    # clean and merged, but new
    assert git_skill.prune_worktrees(repo, keep_last=0, max_age_days=7) == []
    assert wt.is_dir()


def test_keep_last_holds_back_the_newest(repo):
    made = []
    for i in range(3):
        wt = git_skill.create_worktree(repo, f"ai-team/old-{i}")
        _age(wt, 30)
        made.append(wt)

    candidates = git_skill.prunable_worktrees(repo, keep_last=2, max_age_days=1)
    assert len(candidates) == 1, "keep_last did not hold back the newest two"


# --- removals -----------------------------------------------------------


def test_old_clean_merged_worktree_is_pruned(repo):
    wt = git_skill.create_worktree(repo, "ai-team/spent")
    _age(wt, 30)

    removed = git_skill.prune_worktrees(repo, keep_last=0, max_age_days=7)
    assert str(wt) in removed
    assert not wt.exists()
    branches = {w.get("branch") for w in git_skill.list_worktrees(repo)}
    assert "ai-team/spent" not in branches


def test_worktree_is_prunable_once_its_commits_are_merged(repo):
    """The other half of the unmerged check: merged work is safe to drop.

    Detection previously asked about HEAD from the main checkout, so it reported
    zero unmerged commits for everything — which made committed, unmerged work
    look prunable.
    """
    wt = git_skill.create_worktree(repo, "ai-team/merged")
    (wt / "feature.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", "agent work", cwd=wt)
    assert git_skill.worktree_status(repo, wt)["unmerged"] == 1

    _git("merge", "-q", "ai-team/merged", cwd=repo)
    assert git_skill.worktree_status(repo, wt)["unmerged"] == 0

    _age(wt, 30)
    assert str(wt) in git_skill.prune_worktrees(repo, keep_last=0, max_age_days=7)
    assert not wt.exists()


def test_dry_run_reports_without_deleting(repo):
    wt = git_skill.create_worktree(repo, "ai-team/dry")
    _age(wt, 30)

    reported = git_skill.prune_worktrees(repo, keep_last=0, max_age_days=7, dry_run=True)
    assert str(wt) in reported
    assert wt.is_dir(), "dry run deleted something"


def test_prune_is_safe_on_a_repo_with_no_worktrees(repo):
    assert git_skill.prune_worktrees(repo, keep_last=0, max_age_days=0) == []


def test_status_reports_branch_and_age(repo):
    wt = git_skill.create_worktree(repo, "ai-team/inspect")
    _age(wt, 3)
    st = git_skill.worktree_status(repo, wt)
    assert st["branch"] == "ai-team/inspect"
    assert 2.5 < st["age_days"] < 3.5
    assert st["exists"] is True


def test_status_of_a_removed_worktree_reports_missing(repo):
    wt = git_skill.create_worktree(repo, "ai-team/gone")
    git_skill.remove_worktree(repo, wt, force=True)
    assert git_skill.worktree_status(repo, wt)["exists"] is False
