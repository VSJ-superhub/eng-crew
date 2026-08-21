"""A run's output must end up as a commit, not as loose uncommitted files.

The single-agent tier edits files and never commits. Without a commit step the
result of a run is only reachable as `git status` noise in a worktree: not
reviewable as a diff, and never safe to prune.
"""
from __future__ import annotations

import subprocess

import pytest

from eng_crew import git_skill, pipeline, tracker
from eng_crew.config import Settings


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


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()


def _run(monkeypatch, repo, settings, *, edit=True, verified=True):
    """Run the pipeline with a stubbed graph that edits a file. No LLM calls."""
    captured: dict = {}

    class FakeGraph:
        def invoke(self, state):
            captured.update(state)
            if edit:
                work = state["project_path"]
                (repo.__class__(work) / "feature.py").write_text("x = 1\n", encoding="utf-8")
            return {
                **state,
                "final_summary": "agent done",
                "verification_passed": verified,
            }

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: FakeGraph())
    state = pipeline.run_pipeline(task="add a feature", project_path=str(repo), settings=settings)
    return captured, state


def test_run_output_is_committed_in_the_worktree(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True
    settings.commit_run_output = True

    captured, _ = _run(monkeypatch, repo, settings)
    wt = repo.__class__(captured["worktree_path"])

    # the agent's file is committed, not left dangling
    assert _git("status", "--porcelain", cwd=wt).stdout.strip() == ""
    log = _git("log", "--oneline", "-1", cwd=wt).stdout
    assert "eng-crew" in log and "add a feature" in log


def test_commit_message_records_the_run_and_outcome(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True

    captured, _ = _run(monkeypatch, repo, settings, verified=True)
    wt = repo.__class__(captured["worktree_path"])
    body = _git("log", "-1", "--format=%B", cwd=wt).stdout
    assert "Run " in body
    assert "verified" in body


def test_failed_verification_is_still_committed_and_says_so(repo, clean_db, monkeypatch):
    """Failed work is kept: its branch stays unmerged, which is what protects it."""
    settings = Settings()
    settings.worktree_isolation = True

    captured, _ = _run(monkeypatch, repo, settings, verified=False)
    wt = repo.__class__(captured["worktree_path"])
    assert _git("status", "--porcelain", cwd=wt).stdout.strip() == ""
    assert "FAILED" in _git("log", "-1", "--format=%B", cwd=wt).stdout


def test_committing_can_be_turned_off(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True
    settings.commit_run_output = False

    captured, _ = _run(monkeypatch, repo, settings)
    wt = repo.__class__(captured["worktree_path"])
    assert _git("status", "--porcelain", cwd=wt).stdout.strip() != ""


def test_a_run_that_changed_nothing_does_not_fail(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True

    captured, state = _run(monkeypatch, repo, settings, edit=False)
    # no commit to make, and the run still completes normally
    assert state["final_summary"]


def test_a_commit_failure_does_not_break_the_run(repo, clean_db, monkeypatch):
    """An unconfigured git identity is common on a fresh machine.

    The work stays on disk either way; failing the whole run over it would be
    worse than leaving the changes uncommitted.
    """
    _git("config", "--unset", "user.email", cwd=repo)
    _git("config", "--unset", "user.name", cwd=repo)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "")

    settings = Settings()
    settings.worktree_isolation = True
    captured, state = _run(monkeypatch, repo, settings)

    # run completed, and the agent's file is still there to recover
    assert state["final_summary"]
    assert (repo.__class__(captured["worktree_path"]) / "feature.py").exists()


def test_committed_output_becomes_prunable_once_merged(repo, clean_db, monkeypatch):
    """The point of committing: the worktree can eventually be cleaned up.

    Uncommitted output is never prunable, by design — so before this, every
    single-agent worktree accumulated forever.
    """
    import os
    import time

    settings = Settings()
    settings.worktree_isolation = True
    captured, _ = _run(monkeypatch, repo, settings)
    wt = repo.__class__(captured["worktree_path"])
    branch = captured["git_branch"]

    status = git_skill.worktree_status(repo, wt)
    assert status["dirty"] is False
    assert status["unmerged"] == 1, "the run's commit should be unmerged at first"
    # ...and therefore protected from pruning
    assert git_skill.prune_worktrees(repo, keep_last=0, max_age_days=0) == []

    _git("merge", "-q", branch, cwd=repo)
    old = time.time() - 30 * 86400
    os.utime(wt, (old, old))

    assert git_skill.worktree_status(repo, wt)["unmerged"] == 0
    assert str(wt) in git_skill.prune_worktrees(repo, keep_last=0, max_age_days=7)
