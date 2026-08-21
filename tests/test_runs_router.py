"""Run lifecycle endpoints.

These are the endpoints that spend money: they launch builds. That makes the
guards in front of them the valuable part — an endpoint that starts a second
concurrent run on the same project, or relaunches a deleted backlog item, costs
real API spend to discover in production.

Every test that touches a launch path stubs the launcher AND asserts the stub
was reached. Without that assertion a missed patch would start a real run and
the test would still pass.
"""
from __future__ import annotations

import subprocess
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tracker  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
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
    return {"id": resp.json()["id"], "path": str(root)}


@pytest.fixture
def backlog_item(client, project):
    resp = client.post("/api/backlog", json={
        "title": "build a thing",
        "description": "details",
        "project_path": project["path"],
    })
    return resp.json()["id"]


@pytest.fixture
def no_real_launches(monkeypatch):
    """Stub the subprocess launcher. Records calls; never starts anything."""
    calls: list = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append({"cmd": cmd, "kwargs": kwargs})
            self.pid = 4242

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return calls


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- launching from the backlog -----------------------------------------


def test_running_an_unknown_backlog_item_is_a_404(client, no_real_launches):
    assert client.post("/api/backlog/999999/run").status_code == 404
    assert no_real_launches == []


def test_an_item_already_running_is_not_launched_again(client, backlog_item, no_real_launches):
    tracker.update_backlog_item(backlog_item, status="running")
    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 400
    assert "already running" in resp.json()["error"]
    assert no_real_launches == [], "a duplicate launch was started"


def test_a_second_run_on_the_same_project_is_refused(client, project, backlog_item, no_real_launches):
    # An active run for this project already exists.
    tracker.create_run("something else", project["path"])

    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 409
    assert "Already running" in resp.json()["error"]
    assert no_real_launches == [], "a concurrent run was started for the same project"


def test_launching_marks_the_item_running_and_starts_one_process(
    client, project, backlog_item, no_real_launches
):
    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert _wait_for(lambda: len(no_real_launches) == 1), "the launcher was never reached"
    cmd = no_real_launches[0]["cmd"]
    assert "eng_crew" in cmd and "run" in cmd
    assert project["path"] in cmd
    # the task text handed to the builder is the item's title and description
    assert any("build a thing" in str(part) for part in cmd)

    item = tracker.get_backlog_item(backlog_item)
    assert item["status"] == "running"


def test_launching_opens_a_sprint_for_the_item(client, project, backlog_item, no_real_launches):
    client.post(f"/api/backlog/{backlog_item}/run")
    assert _wait_for(lambda: len(no_real_launches) == 1)
    sprints = client.get(f"/api/projects/{project['id']}/sprints").json()["sprints"]
    assert len(sprints) == 1


# --- retry --------------------------------------------------------------


def test_retrying_an_unknown_run_is_a_404(client):
    assert client.post("/api/runs/999999/retry").status_code == 404


def test_retry_relaunches_the_original_task(client, project, monkeypatch):
    run_id = tracker.create_run("the original task", project["path"])

    started: list = []
    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "run_task", lambda **kw: started.append(kw))

    assert client.post(f"/api/runs/{run_id}/retry").status_code == 200
    assert _wait_for(lambda: started), "retry never reached the builder"
    assert started[0]["task"] == "the original task"
    assert started[0]["project_path"] == project["path"]


# --- approval bridge ----------------------------------------------------


def test_approving_a_run_records_the_decision_for_the_pipeline(client, project):
    run_id = tracker.create_run("t", project["path"])

    resp = client.post(f"/api/runs/{run_id}/approve", json={"approved": True})
    assert resp.status_code == 200

    # The pipeline's HITL gate polls the DB for this; it is the whole bridge.
    decision = tracker.get_hitl_decision(run_id)
    assert decision["approved"] is True


def test_rejecting_a_run_carries_the_feedback_through(client, project):
    run_id = tracker.create_run("t", project["path"])
    client.post(f"/api/runs/{run_id}/approve", json={
        "approved": False,
        "feedback": "split this into two",
    })
    decision = tracker.get_hitl_decision(run_id)
    assert decision["approved"] is False
    assert decision["feedback"] == "split this into two"


def test_awaiting_approval_lists_runs_in_that_state(client, project):
    run_id = tracker.create_run("t", project["path"])
    assert client.get("/api/runs/awaiting-approval").json() == []

    tracker.update_run_status(run_id, "awaiting_approval")
    assert client.get("/api/runs/awaiting-approval").json() == [run_id]


# --- pause / resume / cancel --------------------------------------------


def test_pause_records_the_request(client, project):
    """Pause records a flag. Note it is not currently honoured anywhere:
    tracker.is_pause_requested() exists but no pipeline code calls it, so the
    button is inert. This pins what the endpoint actually does today.
    """
    run_id = tracker.create_run("t", project["path"])
    assert client.post(f"/api/runs/{run_id}/pause").status_code == 200
    assert tracker.is_pause_requested(run_id) is True


def test_resume_reports_a_conflict_when_state_is_gone(client, project, monkeypatch):
    run_id = tracker.create_run("t", project["path"])
    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "resume_run", lambda rid: False)
    resp = client.post(f"/api/runs/{run_id}/resume")
    assert resp.status_code == 409
    assert "State lost" in resp.json()["error"]


def test_resume_succeeds_when_the_run_can_be_resumed(client, project, monkeypatch):
    run_id = tracker.create_run("t", project["path"])
    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "resume_run", lambda rid: True)
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 200


def test_cancel_finishes_the_run_with_a_reason(client, project):
    run_id = tracker.create_run("t", project["path"])
    client.post(f"/api/runs/{run_id}/cancel")

    run = client.get(f"/api/run/{run_id}").json()["run"]
    assert run["status"] == "failed"
    assert "Cancelled" in run["final_summary"]


# --- clarify and sprints ------------------------------------------------


def test_clarifying_an_unknown_run_is_a_404(client):
    resp = client.post("/api/run/999999/clarify", json={"answer": "yes"})
    assert resp.status_code == 404


def test_reading_an_unknown_sprint_is_a_404(client):
    assert client.get("/api/sprints/999999").status_code == 404


def test_subtask_review_is_accepted_as_a_no_op(client, project):
    # Documented as inert; the frontend still posts to it.
    run_id = tracker.create_run("t", project["path"])
    resp = client.post(f"/api/runs/{run_id}/subtask-review", json={
        "subtask_idx": 0, "approved": True,
    })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# --- issues -------------------------------------------------------------


def test_running_an_unknown_issue_is_a_404(client, no_real_launches):
    assert client.post("/api/issues/999999/run").status_code == 404
    assert no_real_launches == []


def test_resume_releases_a_live_paused_run_without_relaunching(client, project, monkeypatch):
    """Resuming a paused run clears the flag; restarting would run it twice."""
    run_id = tracker.create_run("t", project["path"])
    client.post(f"/api/runs/{run_id}/pause")
    assert tracker.is_pause_requested(run_id) is True

    relaunched: list = []
    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "resume_run", lambda rid: relaunched.append(rid) or True)

    resp = client.post(f"/api/runs/{run_id}/resume")
    assert resp.status_code == 200
    assert resp.json()["unpaused"] is True
    assert tracker.is_pause_requested(run_id) is False
    assert relaunched == [], "resume restarted the task instead of releasing it"
