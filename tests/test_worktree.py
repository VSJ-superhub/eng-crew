"""Worktree isolation: a run must not disturb the checkout you are working in.

Before this, every run called ensure_branch(), which stashed uncommitted changes
and switched the main checkout onto a new branch. These tests pin the guarantee
that a run leaves the developer's branch, working tree, and stash alone.
"""
from __future__ import annotations

import subprocess

import pytest

from eng_crew import git_skill


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A repo with one commit, a .venv, and uncommitted work in progress."""
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker.txt").write_text("deps", encoding="utf-8")
    _git("add", "app.py", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    # the developer is mid-edit
    (root / "app.py").write_text("print('UNCOMMITTED WIP')\n", encoding="utf-8")
    return root


def test_worktree_is_created_on_its_own_branch(repo):
    wt = git_skill.create_worktree(repo, "ai-team/test-1")
    assert wt.is_dir()
    assert git_skill.current_branch(wt) == "ai-team/test-1"


def test_main_checkout_keeps_its_branch(repo):
    git_skill.create_worktree(repo, "ai-team/test-2")
    assert git_skill.current_branch(repo) == "main"


def test_uncommitted_work_is_left_alone(repo):
    before = (repo / "app.py").read_text(encoding="utf-8")
    wt = git_skill.create_worktree(repo, "ai-team/test-3")
    (wt / "app.py").write_text("print('agent change')\n", encoding="utf-8")
    assert (repo / "app.py").read_text(encoding="utf-8") == before


def test_nothing_is_stashed(repo):
    git_skill.create_worktree(repo, "ai-team/test-4")
    assert _git("stash", "list", cwd=repo).stdout.strip() == ""


def test_worktree_lives_outside_the_repo(repo):
    # Inside the repo it would appear as untracked files in the main checkout.
    wt = git_skill.create_worktree(repo, "ai-team/test-5")
    assert repo.resolve() not in wt.resolve().parents


def test_edits_in_the_worktree_do_not_reach_the_main_checkout(repo):
    wt = git_skill.create_worktree(repo, "ai-team/test-6")
    (wt / "brand_new.py").write_text("x = 1\n", encoding="utf-8")
    assert not (repo / "brand_new.py").exists()


def test_dependency_dirs_are_linked_in(repo):
    wt = git_skill.create_worktree(repo, "ai-team/test-7")
    linked = git_skill.link_into_worktree(repo, wt, [".venv", "node_modules"])
    assert ".venv" in linked
    # A fresh worktree has no .venv, so tests could not otherwise run there.
    assert (wt / ".venv" / "marker.txt").exists()


def test_linking_skips_absent_directories(repo):
    wt = git_skill.create_worktree(repo, "ai-team/test-8")
    linked = git_skill.link_into_worktree(repo, wt, ["node_modules", "nope"])
    assert linked == []


def test_worktrees_are_listed_and_removable(repo):
    wt = git_skill.create_worktree(repo, "ai-team/test-9")
    branches = {w.get("branch") for w in git_skill.list_worktrees(repo)}
    assert "ai-team/test-9" in branches

    git_skill.remove_worktree(repo, wt, force=True)
    branches = {w.get("branch") for w in git_skill.list_worktrees(repo)}
    assert "ai-team/test-9" not in branches


def test_duplicate_worktree_path_is_refused(repo):
    git_skill.create_worktree(repo, "ai-team/test-10")
    with pytest.raises(git_skill.GitError):
        git_skill.create_worktree(repo, "ai-team/test-10")


# --- pipeline integration (no LLM calls) --------------------------------


def test_pipeline_runs_agents_inside_the_worktree(repo, tmp_path, monkeypatch):
    """run_pipeline must hand agents the worktree path, not the project path."""
    from eng_crew import pipeline, tracker
    from eng_crew.config import Settings

    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()

    seen: dict = {}

    class FakeGraph:
        def invoke(self, state):
            seen.update(state)
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda settings: FakeGraph())

    settings = Settings()
    settings.worktree_isolation = True
    pipeline.run_pipeline(task="do a thing", project_path=str(repo), settings=settings)

    assert seen["worktree_path"], "no worktree was created"
    assert seen["project_path"] == seen["worktree_path"], "agents got the main checkout"
    assert seen["main_project_path"] == str(repo)
    # and the developer's checkout is still untouched
    assert git_skill.current_branch(repo) == "main"
    assert "UNCOMMITTED WIP" in (repo / "app.py").read_text(encoding="utf-8")


def test_pipeline_falls_back_when_isolation_is_off(repo, tmp_path, monkeypatch):
    from eng_crew import pipeline, tracker
    from eng_crew.config import Settings

    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()

    seen: dict = {}

    class FakeGraph:
        def invoke(self, state):
            seen.update(state)
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda settings: FakeGraph())

    settings = Settings()
    settings.worktree_isolation = False
    pipeline.run_pipeline(task="do a thing", project_path=str(repo), settings=settings)

    assert seen["worktree_path"] is None
    assert seen["project_path"] == str(repo)
