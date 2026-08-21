"""Planning and architecture endpoints.

Planning is the expensive half of the dashboard: each of these launches a
background run that costs money, and the guard against launching a second one
while the first is still thinking is the only thing standing between a stray
double-click and two concurrent planning runs.

As elsewhere, every test on a launch path stubs the launcher and asserts the
stub was reached — a missed patch would start a real run and still pass.
"""
from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tracker  # noqa: E402
from eng_crew.dashboard.routers import intake, projects as projects_router  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "plan.db")
    tracker.close_connection()
    tracker._init_db()
    from eng_crew.dashboard.app import app

    with TestClient(app) as c:
        yield c
    tracker.close_connection()


@pytest.fixture
def project(client, tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    resp = client.post("/api/projects", json={"name": "proj", "project_path": str(root)})
    return {"id": resp.json()["id"], "path": root}


@pytest.fixture
def no_planning_runs(monkeypatch):
    """Stub the planning launcher. Records calls; never starts a run."""
    calls: list = []
    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "run_project", lambda **kw: calls.append(kw))
    monkeypatch.setattr(run_mod, "run_sprint", lambda sid: calls.append({"sprint": sid}))
    return calls


def _wait_for(predicate, timeout=2.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- the planning brief parser (pure) -----------------------------------


BRIEF = """# My Project

Some intro prose that is not part of the brief.

## What Needs to Be Built
- a renamer
- a CLI

## Installation
pip install thing

## Technical Constraints
- python 3.11 only
"""


def test_brief_keeps_only_the_planning_sections(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(BRIEF, encoding="utf-8")

    brief = projects_router._extract_planning_brief(str(md))

    assert "a renamer" in brief
    assert "python 3.11 only" in brief
    # Unrelated sections must not be fed to the planner as if they were work.
    assert "pip install thing" not in brief
    assert "Some intro prose" not in brief


def test_a_section_ends_at_the_next_unrelated_heading(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(BRIEF, encoding="utf-8")
    brief = projects_router._extract_planning_brief(str(md))
    built = brief.split("## Technical Constraints")[0]
    assert "pip install" not in built


def test_a_file_with_no_planning_sections_is_used_whole(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# Notes\n\njust some prose\n", encoding="utf-8")
    assert "just some prose" in projects_router._extract_planning_brief(str(md))


def test_a_missing_file_yields_an_empty_brief(tmp_path):
    assert projects_router._extract_planning_brief(str(tmp_path / "nope.md")) == ""


# --- architecture -------------------------------------------------------


def test_architecture_of_an_unknown_project_is_a_404(client):
    assert client.get("/api/projects/999999/architecture").status_code == 404


def test_architecture_reports_a_missing_file_without_failing(client, project):
    body = client.get(f"/api/projects/{project['id']}/architecture").json()
    assert body["content"] is None
    assert "File not found" in body["error"]


def test_architecture_returns_the_file(client, project):
    (project["path"] / "CLAUDE.md").write_text("# The Design\n", encoding="utf-8")
    body = client.get(f"/api/projects/{project['id']}/architecture").json()
    assert body["content"] == "# The Design\n"
    assert body["filename"] == "CLAUDE.md"


def test_updating_architecture_writes_what_the_model_streamed(client, project, monkeypatch):
    async def fake_stream(prompt):
        yield 'data: {"text": "# Updated"}\n\n'
        yield 'data: {"text": " Architecture"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(intake, "_run_claude_streaming", fake_stream)

    with client.stream("POST", f"/api/projects/{project['id']}/update-architecture") as r:
        body = r.read().decode()

    assert "Updated" in body
    # The endpoint's real job: persist the streamed result, not just relay it.
    written = (project["path"] / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert written == "# Updated Architecture"


def test_updating_architecture_for_an_unknown_project_is_a_404(client):
    assert client.post("/api/projects/999999/update-architecture").status_code == 404


# --- starting a plan ----------------------------------------------------


def test_planning_an_unknown_project_is_a_404(client, no_planning_runs):
    assert client.post("/api/projects/999999/plan", json={"goal": "g"}).status_code == 404
    assert no_planning_runs == []


def test_creating_a_plan_launches_one_planning_run(client, project, no_planning_runs):
    resp = client.post(f"/api/projects/{project['id']}/plan", json={"goal": "ship v2"})
    assert resp.status_code == 200
    plan_id = resp.json()["plan_id"]
    assert resp.json()["sprints"] == []

    assert _wait_for(lambda: no_planning_runs), "the planner was never reached"
    launched = no_planning_runs[0]
    assert launched["goal"] == "ship v2"
    assert launched["plan_id"] == plan_id


def test_a_second_plan_is_refused_while_the_first_is_still_planning(
    client, project, no_planning_runs
):
    client.post(f"/api/projects/{project['id']}/plan", json={"goal": "first"})
    assert _wait_for(lambda: no_planning_runs)
    no_planning_runs.clear()

    resp = client.post(f"/api/projects/{project['id']}/plan", json={"goal": "second"})
    assert resp.status_code == 409
    assert "Planning in progress" in resp.json()["error"]
    assert no_planning_runs == [], "a concurrent planning run was started"


def test_force_overrides_the_in_progress_guard(client, project, no_planning_runs):
    client.post(f"/api/projects/{project['id']}/plan", json={"goal": "first"})
    assert _wait_for(lambda: no_planning_runs)
    no_planning_runs.clear()

    resp = client.post(f"/api/projects/{project['id']}/plan",
                       json={"goal": "second", "force": True})
    assert resp.status_code == 200
    assert _wait_for(lambda: no_planning_runs)


def test_a_finished_plan_does_not_block_a_new_one(client, project, no_planning_runs):
    first = client.post(f"/api/projects/{project['id']}/plan", json={"goal": "first"}).json()
    tracker.add_plan_sprint(first["plan_id"], 1, "sprint one", "do a thing")
    assert _wait_for(lambda: no_planning_runs)
    no_planning_runs.clear()

    # The guard is "planning in progress", not "a plan exists".
    resp = client.post(f"/api/projects/{project['id']}/plan", json={"goal": "second"})
    assert resp.status_code == 200


# --- planning from CLAUDE.md --------------------------------------------


def test_planning_from_claude_md_feeds_the_brief_to_the_planner(
    client, project, no_planning_runs
):
    (project["path"] / "CLAUDE.md").write_text(BRIEF, encoding="utf-8")

    resp = client.post(f"/api/projects/{project['id']}/plan-from-claude-md")
    assert resp.status_code == 200
    assert resp.json()["source"] == "claude_md"

    assert _wait_for(lambda: no_planning_runs), "the planner was never reached"
    goal = no_planning_runs[0]["goal"]
    assert "a renamer" in goal
    assert "pip install thing" not in goal, "unrelated sections leaked into the goal"


def test_planning_from_claude_md_on_an_unknown_project_is_a_404(client, no_planning_runs):
    assert client.post("/api/projects/999999/plan-from-claude-md").status_code == 404
    assert no_planning_runs == []


def test_planning_from_claude_md_respects_the_in_progress_guard(
    client, project, no_planning_runs
):
    (project["path"] / "CLAUDE.md").write_text(BRIEF, encoding="utf-8")
    client.post(f"/api/projects/{project['id']}/plan-from-claude-md")
    assert _wait_for(lambda: no_planning_runs)
    no_planning_runs.clear()

    resp = client.post(f"/api/projects/{project['id']}/plan-from-claude-md")
    assert resp.status_code == 409
    assert no_planning_runs == []


# --- reading a plan -----------------------------------------------------


def test_a_project_with_no_plan_reports_nothing(client, project):
    body = client.get(f"/api/projects/{project['id']}/plan").json()
    assert body == {"plan": None, "sprints": [], "planning": False}


def test_a_plan_without_sprints_reads_as_still_planning(client, project, no_planning_runs):
    client.post(f"/api/projects/{project['id']}/plan", json={"goal": "g"})
    body = client.get(f"/api/projects/{project['id']}/plan").json()
    assert body["planning"] is True
    assert body["sprints"] == []


def test_a_plan_with_sprints_is_no_longer_planning(client, project, no_planning_runs):
    plan_id = client.post(f"/api/projects/{project['id']}/plan",
                          json={"goal": "g"}).json()["plan_id"]
    tracker.add_plan_sprint(plan_id, 1, "sprint one", "do a thing")

    body = client.get(f"/api/projects/{project['id']}/plan").json()
    assert body["planning"] is False
    assert [s["name"] for s in body["sprints"]] == ["sprint one"]


def test_a_plan_stuck_without_sprints_is_reported_as_failed(client, project, no_planning_runs):
    """A planner that died leaves a plan with no sprints; after five minutes
    the dashboard must say so rather than spin forever."""
    plan_id = client.post(f"/api/projects/{project['id']}/plan",
                          json={"goal": "g"}).json()["plan_id"]

    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = tracker._connect()
    with conn:
        conn.execute("UPDATE project_plans SET created_at=? WHERE id=?", (stale, plan_id))

    body = client.get(f"/api/projects/{project['id']}/plan").json()
    assert body["planning"] is False
    assert body["planning_failed"] is True


# --- plan sprints -------------------------------------------------------


def test_sprint_tasks_are_empty_for_an_unknown_sprint(client):
    assert client.get("/api/plan-sprints/999999/tasks").json() == {"tasks": []}


def test_updating_a_sprint_persists(client, project, no_planning_runs):
    plan_id = client.post(f"/api/projects/{project['id']}/plan",
                          json={"goal": "g"}).json()["plan_id"]
    sprint_id = tracker.add_plan_sprint(plan_id, 1, "original", "d")

    assert client.put(f"/api/plan-sprints/{sprint_id}", json={"name": "renamed"}).status_code == 200
    sprints = client.get(f"/api/projects/{project['id']}/plan").json()["sprints"]
    assert [s["name"] for s in sprints] == ["renamed"]


def test_running_an_unknown_sprint_is_a_404(client, no_planning_runs):
    assert client.post("/api/plan-sprints/999999/run").status_code == 404
    assert no_planning_runs == []


def test_running_a_sprint_launches_it(client, project, no_planning_runs):
    plan_id = client.post(f"/api/projects/{project['id']}/plan",
                          json={"goal": "g"}).json()["plan_id"]
    sprint_id = tracker.add_plan_sprint(plan_id, 1, "sprint one", "d")
    no_planning_runs.clear()

    assert client.post(f"/api/plan-sprints/{sprint_id}/run").status_code == 200
    assert _wait_for(lambda: no_planning_runs), "the sprint runner was never reached"
    assert no_planning_runs[0] == {"sprint": sprint_id}
