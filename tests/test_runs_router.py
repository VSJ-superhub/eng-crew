"""Run lifecycle endpoints.

These are the endpoints that spend money: they launch builds. That makes the
guards in front of them the valuable part — an endpoint that starts a second
concurrent run on the same project, or relaunches a ticket already in flight,
costs real API spend to discover in production.

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
    """A ticket that is ready to pull: it has acceptance criteria and has been
    moved to `ready`, which is the only state work can start from."""
    resp = client.post("/api/backlog", json={
        "title": "build a thing",
        "description": "details",
        "project_path": project["path"],
        "acceptance_criteria": "- the thing is built",
    })
    item_id = resp.json()["id"]
    client.put(f"/api/tickets/{item_id}/status", json={"status": "ready", "actor": "tester"})
    return item_id


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


def test_a_ticket_already_in_flight_is_not_launched_again(client, backlog_item, no_real_launches):
    tracker.update_backlog_item(backlog_item, status="in_progress")
    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 400
    assert "already running" in resp.json()["error"]
    assert no_real_launches == [], "a duplicate launch was started"


def test_a_ticket_that_is_not_ready_cannot_be_launched(client, project, no_real_launches):
    """The lifecycle is the gate: a ticket still in the backlog — which is where
    one with no acceptance criteria is stuck — is not work anyone can start."""
    item_id = client.post("/api/backlog", json={
        "title": "half an idea",
        "project_path": project["path"],
    }).json()["id"]

    resp = client.post(f"/api/backlog/{item_id}/run")
    assert resp.status_code == 400
    assert "backlog" in resp.json()["error"]
    assert no_real_launches == [], "an unready ticket was built"


def test_a_second_run_on_the_same_project_is_refused(client, project, backlog_item, no_real_launches):
    # An active run for this project already exists.
    tracker.create_run("something else", project["path"])

    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 409
    assert "Already running" in resp.json()["error"]
    assert no_real_launches == [], "a concurrent run was started for the same project"


def test_launching_claims_the_ticket_and_starts_one_process(
    client, project, backlog_item, no_real_launches
):
    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    assert _wait_for(lambda: len(no_real_launches) == 1), "the launcher was never reached"
    cmd = no_real_launches[0]["cmd"]
    # The builder is pointed at the ticket, not at a task string assembled here:
    # it reads the ticket's own title, description, and acceptance criteria.
    assert "eng_crew" in cmd and "ticket" in cmd
    assert str(backlog_item) in [str(part) for part in cmd]

    item = tracker.get_backlog_item(backlog_item)
    assert item["status"] == "in_progress"


def test_launching_records_who_claimed_the_ticket(client, backlog_item, no_real_launches):
    client.post(f"/api/backlog/{backlog_item}/run")
    assert _wait_for(lambda: len(no_real_launches) == 1)

    events = client.get(f"/api/tickets/{backlog_item}/events").json()["events"]
    claim = events[-1]
    assert (claim["from_status"], claim["to_status"]) == ("ready", "in_progress")
    assert claim["actor"], "a status change with no actor is not worth recording"


def test_the_wip_limit_stops_a_launch(client, project, backlog_item, no_real_launches,
                                      monkeypatch):
    """The queue is continuous, so the WIP limit is the only backpressure there
    is — a launch past it must be refused rather than queued up."""
    from eng_crew import tickets

    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 1)
    other = client.post("/api/backlog", json={
        "title": "already in flight",
        "project_path": project["path"],
        "acceptance_criteria": "- done",
    }).json()["id"]
    tracker.update_backlog_item(other, status="in_progress")

    resp = client.post(f"/api/backlog/{backlog_item}/run")
    assert resp.status_code == 409
    assert "WIP limit" in resp.json()["error"]
    assert no_real_launches == [], "a launch was started past the WIP limit"
    assert tracker.get_backlog_item(backlog_item)["status"] == "ready"


# --- running queued work ------------------------------------------------
#
# The bug this replaced launched the literal string "Execute sprint N". Queued
# work now means: take the next ready ticket and build that.


def ready_ticket(client, project, title, priority=50):
    item_id = client.post("/api/backlog", json={
        "title": title,
        "project_path": project["path"],
        "priority": priority,
        "acceptance_criteria": "- it works",
    }).json()["id"]
    client.put(f"/api/tickets/{item_id}/status", json={"status": "ready", "actor": "tester"})
    return item_id


def test_running_the_queue_builds_the_highest_priority_ticket(
    client, project, no_real_launches
):
    ready_ticket(client, project, "later", priority=90)
    first = ready_ticket(client, project, "sooner", priority=10)

    body = client.post(f"/api/projects/{project['id']}/queue/run").json()

    assert [s["ticket_id"] for s in body["started"]] == [first]
    assert body["waiting"] == 1, "the rest of the queue should still be waiting"
    assert _wait_for(lambda: len(no_real_launches) == 1), "the builder was never reached"
    assert str(first) in [str(part) for part in no_real_launches[0]["cmd"]]
    assert tracker.get_backlog_item(first)["status"] == "in_progress"


def test_running_the_queue_starts_one_build_at_a_time(client, project, no_real_launches):
    """A project builds one ticket at a time — runs share its checkout. The WIP
    limit bounds tickets in flight, which is a different thing."""
    ready_ticket(client, project, "one", priority=10)
    ready_ticket(client, project, "two", priority=20)

    client.post(f"/api/projects/{project['id']}/queue/run")
    assert _wait_for(lambda: len(no_real_launches) == 1)

    resp = client.post(f"/api/projects/{project['id']}/queue/run")
    assert resp.status_code == 409
    assert "Already running" in resp.json()["error"]
    assert len(no_real_launches) == 1, "a second concurrent build was started"


def test_running_an_empty_queue_starts_nothing(client, project, no_real_launches):
    body = client.post(f"/api/projects/{project['id']}/queue/run").json()
    assert body["started"] == []
    assert body["waiting"] == 0
    assert no_real_launches == []


def test_a_backlog_ticket_is_not_queued_work(client, project, no_real_launches):
    """Only `ready` tickets are pullable, which is what the acceptance-criteria
    gate is for: work with no definition of done never reaches the queue."""
    client.post("/api/backlog", json={"title": "vague idea", "project_path": project["path"]})

    body = client.post(f"/api/projects/{project['id']}/queue/run").json()
    assert body["started"] == []
    assert body["waiting"] == 0
    assert no_real_launches == []


def test_running_the_queue_at_the_wip_limit_is_refused(
    client, project, no_real_launches, monkeypatch
):
    from eng_crew import tickets

    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 1)
    held = ready_ticket(client, project, "in flight")
    tracker.update_backlog_item(held, status="in_review")
    ready_ticket(client, project, "next up")

    resp = client.post(f"/api/projects/{project['id']}/queue/run")
    assert resp.status_code == 409
    assert "WIP limit" in resp.json()["error"]
    assert no_real_launches == [], "a build started past the WIP limit"


def test_running_the_queue_of_an_unknown_project_is_a_404(client, no_real_launches):
    assert client.post("/api/projects/999999/queue/run").status_code == 404
    assert no_real_launches == []


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
