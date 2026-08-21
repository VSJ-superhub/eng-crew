"""Dashboard HTTP API tests.

The dashboard is the surface people actually look at, and it was the least
tested part of the codebase. These run the real FastAPI app against an isolated
tracker database — no mocked routers — so they exercise the request/response
contract the frontend depends on.

Deliberately not covered here: endpoints that shell out to an LLM (intake,
manager), endpoints that start a pipeline run, and the SSE streams. Those need
process control rather than a request/response assertion.
"""
from __future__ import annotations

import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tracker  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The real app, pointed at an empty database."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "dash.db")
    tracker.close_connection()
    tracker._init_db()

    from eng_crew.dashboard.app import app

    with TestClient(app) as c:
        yield c
    tracker.close_connection()


@pytest.fixture
def project(client, tmp_path):
    """A registered project, pointing at a real directory."""
    root = tmp_path / "someproject"
    root.mkdir()
    resp = client.post("/api/projects", json={
        "name": "someproject",
        "project_path": str(root),
    })
    assert resp.status_code == 200, resp.text
    return {"id": resp.json()["id"], "path": str(root)}


# --- app wiring ---------------------------------------------------------


def test_app_exposes_its_routes():
    from eng_crew.dashboard.app import app

    paths = set(app.openapi()["paths"])
    for expected in ("/api/projects", "/api/backlog", "/api/providers"):
        assert expected in paths, f"{expected} is not registered"


def test_unknown_route_is_a_404(client):
    assert client.get("/api/definitely-not-a-route").status_code == 404


# --- system -------------------------------------------------------------


def test_providers_reports_credential_availability_per_provider(client):
    resp = client.get("/api/providers")
    assert resp.status_code == 200
    body = resp.json()
    # The frontend switches on these booleans to grey out unusable stacks.
    assert "claude_cli" in body
    assert all(isinstance(v, bool) for v in body.values()), body


def test_entroly_status_answers_even_when_entroly_is_absent(client):
    # The dashboard must render whether or not the optional binary is installed.
    resp = client.get("/api/entroly/status")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"enabled", "available"}
    assert isinstance(body["available"], bool)


def test_claude_usage_stats_answer(client):
    resp = client.get("/api/stats/claude-usage")
    assert resp.status_code == 200


# --- projects -----------------------------------------------------------


def test_projects_list_is_empty_before_anything_is_registered(client):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_then_read_a_project(client, project):
    listed = client.get("/api/projects").json()
    assert [p["id"] for p in listed] == [project["id"]]

    detail = client.get(f"/api/projects/{project['id']}")
    assert detail.status_code == 200
    assert detail.json()["project_path"] == project["path"]


def test_creating_a_project_on_a_missing_folder_is_rejected(client, tmp_path):
    resp = client.post("/api/projects", json={
        "name": "ghost",
        "project_path": str(tmp_path / "does-not-exist"),
    })
    assert resp.status_code == 400
    assert "not found" in resp.json()["error"].lower()


def test_reading_an_unknown_project_is_a_404(client):
    assert client.get("/api/projects/999999").status_code == 404


def test_update_a_project(client, project):
    resp = client.put(f"/api/projects/{project['id']}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert client.get(f"/api/projects/{project['id']}").json()["name"] == "renamed"


def test_delete_a_project(client, project):
    assert client.delete(f"/api/projects/{project['id']}").status_code == 200
    assert client.get("/api/projects").json() == []


def test_project_runs_and_tasks_are_empty_lists_not_errors(client, project):
    for suffix in ("runs", "tasks", "features"):
        resp = client.get(f"/api/projects/{project['id']}/{suffix}")
        assert resp.status_code == 200, f"{suffix}: {resp.text}"
        assert isinstance(resp.json(), (list, dict))


def test_task_summary_answers_with_no_projects(client):
    resp = client.get("/api/projects/task-summary")
    assert resp.status_code == 200


def test_project_id_must_be_an_integer(client):
    # FastAPI path validation, not a 500 from deeper in the stack.
    assert client.get("/api/projects/not-a-number").status_code == 422


# --- backlog ------------------------------------------------------------


def test_backlog_round_trip(client, project):
    created = client.post("/api/backlog", json={
        "title": "do a thing",
        "description": "in detail",
        "project_path": project["path"],
    })
    assert created.status_code == 200
    item_id = created.json()["id"]

    items = client.get("/api/backlog").json()
    assert [i["id"] for i in items] == [item_id]
    assert items[0]["title"] == "do a thing"

    assert client.put(f"/api/backlog/{item_id}", json={"title": "renamed"}).status_code == 200
    assert client.get("/api/backlog").json()[0]["title"] == "renamed"

    assert client.delete(f"/api/backlog/{item_id}").status_code == 200
    assert client.get("/api/backlog").json() == []


def test_backlog_requires_a_title(client):
    assert client.post("/api/backlog", json={"description": "no title"}).status_code == 422


def test_backlog_filters_by_status(client, project):
    client.post("/api/backlog", json={"title": "a", "project_path": project["path"]})
    resp = client.get("/api/backlog", params={"status": "nonexistent-status"})
    assert resp.status_code == 200
    assert resp.json() == []


# --- runs ---------------------------------------------------------------


@pytest.fixture
def run_id(client, project):
    return tracker.create_run("a task", project["path"])


def test_reading_an_unknown_run_is_a_404(client):
    assert client.get("/api/run/999999").status_code == 404


def test_read_a_run(client, run_id):
    resp = client.get(f"/api/run/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    # The frontend reads this envelope, not a bare run row.
    assert set(body) >= {"run", "events", "plan", "cost_by_agent"}
    assert body["run"]["task_text"] == "a task"
    assert body["run"]["status"] == "running"
    assert body["events"] == []


def test_awaiting_approval_is_empty_when_nothing_is_waiting(client, run_id):
    resp = client.get("/api/runs/awaiting-approval")
    assert resp.status_code == 200
    assert resp.json() == []


def test_awaiting_subtask_review_returns_both_lists(client, run_id):
    resp = client.get("/api/runs/awaiting-subtask-review")
    assert resp.status_code == 200
    assert resp.json() == {"run_ids": [], "reviews": []}


def test_pausing_a_run_is_recorded(client, run_id):
    resp = client.post(f"/api/runs/{run_id}/pause")
    assert resp.status_code == 200
    assert client.get(f"/api/run/{run_id}").status_code == 200


def test_cancelling_a_run_marks_it_terminal(client, run_id):
    resp = client.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 200
    status = client.get(f"/api/run/{run_id}").json()["run"]["status"]
    assert status != "running"


def test_run_logs_endpoint_answers_for_a_run_without_logs(client, run_id):
    resp = client.get(f"/api/{run_id}/logs")
    assert resp.status_code in (200, 404), resp.text


# --- filesystem helpers -------------------------------------------------


def test_browse_lists_a_directory(client, tmp_path):
    # Browsing stays unrestricted: you must be able to find a folder before you
    # can register it. It lists names, never file contents.
    (tmp_path / "child").mkdir()
    resp = client.get("/api/fs/browse", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


def test_read_file_inside_a_registered_project(client, project):
    target = pathlib.Path(project["path"]) / "notes.txt"
    target.write_text("hello from disk", encoding="utf-8")
    resp = client.get("/api/fs/read-file", params={"path": str(target)})
    assert resp.status_code == 200
    assert resp.json()["content"] == "hello from disk"


def test_read_file_reports_a_missing_file_without_crashing(client, project):
    missing = pathlib.Path(project["path"]) / "nope.txt"
    resp = client.get("/api/fs/read-file", params={"path": str(missing)})
    assert resp.status_code == 500
    assert "error" in resp.json()


def test_read_file_refuses_a_path_outside_every_project(client, project, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY", encoding="utf-8")

    resp = client.get("/api/fs/read-file", params={"path": str(secret)})
    assert resp.status_code == 403
    assert "PRIVATE KEY" not in resp.text


def test_read_file_refuses_dot_dot_traversal_out_of_a_project(client, project, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET", encoding="utf-8")
    traversal = str(pathlib.Path(project["path"]) / ".." / "outside.txt")

    resp = client.get("/api/fs/read-file", params={"path": traversal})
    assert resp.status_code == 403
    assert "SECRET" not in resp.text


def test_read_file_refuses_a_symlink_that_escapes_a_project(client, project, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("SECRET", encoding="utf-8")
    link = pathlib.Path(project["path"]) / "escape.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")

    resp = client.get("/api/fs/read-file", params={"path": str(link)})
    assert resp.status_code == 403
    assert "SECRET" not in resp.text


def test_read_file_refuses_everything_when_no_project_is_registered(client, tmp_path):
    target = tmp_path / "anything.txt"
    target.write_text("data", encoding="utf-8")
    assert client.get("/api/fs/read-file", params={"path": str(target)}).status_code == 403


def test_write_claude_md_inside_a_registered_project(client, project):
    resp = client.post("/api/fs/write-claude-md", json={
        "path": project["path"],
        "content": "# Project\n",
    })
    assert resp.status_code == 200
    assert (pathlib.Path(project["path"]) / "CLAUDE.md").read_text(encoding="utf-8") == "# Project\n"


def test_write_claude_md_refuses_a_directory_outside_every_project(client, project, tmp_path):
    victim = tmp_path / "somewhere-else"
    victim.mkdir()

    resp = client.post("/api/fs/write-claude-md", json={
        "path": str(victim),
        "content": "# Injected\n",
    })
    assert resp.status_code == 403
    assert not (victim / "CLAUDE.md").exists(), "wrote outside a registered project"


def test_write_claude_md_refuses_traversal(client, project, tmp_path):
    escape = str(pathlib.Path(project["path"]) / "..")
    resp = client.post("/api/fs/write-claude-md", json={"path": escape, "content": "x"})
    assert resp.status_code == 403
    assert not (tmp_path / "CLAUDE.md").exists()


# --- issues and sprints -------------------------------------------------


def test_issues_start_empty_and_carry_the_project(client, project):
    resp = client.get(f"/api/projects/{project['id']}/issues")
    assert resp.status_code == 200
    body = resp.json()
    assert body["issues"] == []
    assert body["project"]["id"] == project["id"]


def test_create_an_issue_and_read_it_back(client, project):
    created = client.post(f"/api/projects/{project['id']}/issues", json={
        "title": "something is broken",
        "description": "steps to reproduce",
    })
    assert created.status_code == 200
    issue_id = created.json()["id"]

    issues = client.get(f"/api/projects/{project['id']}/issues").json()["issues"]
    assert [i["id"] for i in issues] == [issue_id]
    assert issues[0]["title"] == "something is broken"
    # Issues are backlog items tagged as such; the tag is what separates the views.
    assert issues[0]["type"] == "issue"


def test_an_issue_does_not_show_up_as_a_plain_backlog_item(client, project):
    client.post(f"/api/projects/{project['id']}/issues", json={"title": "a bug"})
    plain = [i for i in client.get("/api/backlog").json() if i.get("type") != "issue"]
    assert plain == []


def test_issues_on_an_unknown_project_are_a_404(client):
    assert client.get("/api/projects/999999/issues").status_code == 404
    assert client.post("/api/projects/999999/issues", json={"title": "x"}).status_code == 404


def test_sprints_start_empty(client, project):
    resp = client.get(f"/api/projects/{project['id']}/sprints")
    assert resp.status_code == 200
    assert resp.json()["sprints"] == []


def test_sprints_on_an_unknown_project_are_a_404(client):
    assert client.get("/api/projects/999999/sprints").status_code == 404


def test_creating_an_issue_requires_a_title(client, project):
    resp = client.post(f"/api/projects/{project['id']}/issues", json={"description": "no title"})
    assert resp.status_code == 422
