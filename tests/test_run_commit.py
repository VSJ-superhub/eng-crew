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


def _run(monkeypatch, repo, settings, *, edit=True, verified=True, unverified=False):
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
                "verification_unverified": unverified,
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


# --- status honesty -----------------------------------------------------
#
# Run 100243 edited nothing, had no checks to run, and was recorded
# "completed" — indistinguishable from a run that passed a real suite.


def test_unverified_run_gets_its_own_status(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True

    _run(monkeypatch, repo, settings, verified=True, unverified=True)
    row = tracker.get_run_detail(1)
    assert row["status"] == "unverified", "no checks ran, so nothing vouched for it"


def test_verified_run_is_still_completed(repo, clean_db, monkeypatch):
    settings = Settings()
    settings.worktree_isolation = True

    _run(monkeypatch, repo, settings, verified=True, unverified=False)
    assert tracker.get_run_detail(1)["status"] == "completed"


def test_gate_that_never_reported_is_failed(repo, clean_db, monkeypatch):
    """verification_passed=None must not read as success."""
    settings = Settings()
    settings.worktree_isolation = True

    _run(monkeypatch, repo, settings, verified=None)
    row = tracker.get_run_detail(1)
    assert row["status"] == "failed"
    assert "NOT VERIFIED" in (row["final_summary"] or "")


# --- dependency links stay out of the commit ----------------------------
#
# link_into_worktree symlinks .venv / node_modules into the worktree so tests
# can run. ".venv/" in .gitignore has a trailing slash and matches a directory,
# not the link standing in for one, so `add -A` staged it and runs 100240 and
# 100243 both committed absolute paths from the developer's machine.


def _make_link(target_dir, link_path):
    """Symlink, falling back to a Windows junction. Returns True if it worked."""
    import os
    try:
        os.symlink(target_dir, link_path, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        return subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            capture_output=True, text=True,
        ).returncode == 0
    return False


def test_linked_dependency_dir_is_not_committed(repo, tmp_path):
    deps = tmp_path / "real_node_modules"
    deps.mkdir()
    (deps / "pkg.txt").write_text("dep\n", encoding="utf-8")
    if not _make_link(deps, repo / "node_modules"):
        pytest.skip("this platform allows neither symlinks nor junctions")

    (repo / "src.py").write_text("y = 2\n", encoding="utf-8")
    sha = git_skill.commit_all(repo, "add src")
    assert sha, "the real change must still be committed"

    tree = _git("cat-file", "-p", f"{sha}^{{tree}}", cwd=repo).stdout
    assert "node_modules" not in tree, "the link must not reach the tree"
    assert "src.py" in tree
    assert (repo / "node_modules" / "pkg.txt").exists(), "the link stays on disk"


def test_symlink_to_a_file_is_still_committed(repo, tmp_path):
    """Only directory links are dependency noise; a file symlink is ordinary."""
    target = repo / "app.py"
    if not _make_link(target, repo / "alias.py"):
        pytest.skip("this platform allows neither symlinks nor junctions")
    if (repo / "alias.py").is_dir():
        pytest.skip("link resolved to a directory on this platform")

    sha = git_skill.commit_all(repo, "add alias")
    assert sha
    assert "alias.py" in _git("cat-file", "-p", f"{sha}^{{tree}}", cwd=repo).stdout


def test_commit_all_returns_none_when_only_links_were_staged(repo, tmp_path):
    """The index ends up empty, so there is nothing to commit — not a crash."""
    deps = tmp_path / "real_venv"
    deps.mkdir()
    (deps / "marker.txt").write_text("x\n", encoding="utf-8")
    if not _make_link(deps, repo / ".venv"):
        pytest.skip("this platform allows neither symlinks nor junctions")

    before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert git_skill.commit_all(repo, "nothing real") is None
    assert _git("rev-parse", "HEAD", cwd=repo).stdout.strip() == before


# --- the log a run wrote must be findable afterwards ---------------------
#
# runs.log_path was NULL for every run: mcp_server created logs/run_<ts>.log
# and redirected the subprocess into it, but never told the tracker, so
# GET /api/{run_id}/logs 404'd on "Log file not found for this run" while the
# file sat on disk. The dashboard passed --log-path, which the CLI did not
# accept at all.


def test_log_path_is_recorded_on_the_run(repo, clean_db, monkeypatch, tmp_path):
    settings = Settings()
    settings.worktree_isolation = False
    log = tmp_path / "run_1.log"
    log.write_text("agent output\n", encoding="utf-8")

    class FakeGraph:
        def invoke(self, state):
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: FakeGraph())
    pipeline.run_pipeline(
        task="t", project_path=str(repo), settings=settings, log_path=str(log)
    )
    assert tracker.get_run_detail(1)["log_path"] == str(log)


def test_log_path_is_recorded_on_a_pre_created_run(repo, clean_db, monkeypatch, tmp_path):
    """Discord and the dashboard create the row before the log exists."""
    settings = Settings()
    settings.worktree_isolation = False
    log = tmp_path / "run_2.log"
    log.write_text("x\n", encoding="utf-8")
    run_id = tracker.create_run("t", str(repo))

    class FakeGraph:
        def invoke(self, state):
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: FakeGraph())
    pipeline.run_pipeline(
        task="t", project_path=str(repo), settings=settings,
        run_id=run_id, log_path=str(log),
    )
    assert tracker.get_run_detail(run_id)["log_path"] == str(log)


def test_cli_accepts_the_log_path_flag():
    """The dashboard has always passed --log-path; the CLI must know it."""
    from typer.testing import CliRunner
    from eng_crew.cli import app

    out = CliRunner().invoke(app, ["run", "--help"]).output
    assert "--log-path" in out
