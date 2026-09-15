"""A run must register its project, so list_projects is never silently empty.

Nothing used to insert into `projects` when a run started, even though the MCP
server told users projects were "registered on first run".
"""
from __future__ import annotations

import pytest

from eng_crew import tracker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "my-proj"
    p.mkdir()
    return p


def test_first_run_registers_the_project(db, project):
    tracker.create_run("task", str(project))
    projects = tracker.list_projects(active_only=False)
    assert [p["name"] for p in projects] == ["my-proj"]
    assert projects[0]["claude_md_path"].endswith("CLAUDE.md")


def test_repeat_runs_do_not_duplicate(db, project):
    for _ in range(3):
        tracker.create_run("task", str(project))
    assert len(tracker.list_projects(active_only=False)) == 1


def test_other_path_spelling_matches_existing_row(db, project):
    tracker.create_run("task", str(project).replace("\\", "/"))
    tracker.create_run("task", str(project).replace("/", "\\"))
    assert len(tracker.list_projects(active_only=False)) == 1


def test_manually_registered_project_is_left_alone(db, project):
    pid = tracker.add_project("Custom Name", str(project), str(project / "CLAUDE.md"))
    tracker.create_run("task", str(project))
    projects = tracker.list_projects(active_only=False)
    assert [(p["id"], p["name"]) for p in projects] == [(pid, "Custom Name")]


def test_registration_failure_does_not_break_the_run(db, project, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(tracker, "add_project", boom)
    run_id = tracker.create_run("task", str(project))
    assert run_id
    assert tracker.list_projects(active_only=False) == []


def test_missing_directory_is_not_registered_but_run_still_created(db, tmp_path):
    run_id = tracker.create_run("task", str(tmp_path / "does-not-exist"))
    assert run_id
    assert tracker.list_projects(active_only=False) == []
