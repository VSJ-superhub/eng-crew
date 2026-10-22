"""A ticket can depend on other tickets, and the dependency is respected.

Ordering by priority alone said what to build *first*; it could not say what
must be built *before*. The planner already decomposes a goal into work that
lands in sequence, so the sequence is worth recording: a ticket carries the ids
of the tickets it waits on, a prerequisite counts as met once it is done or
shipped, and everything that pulls or starts work honours that — the queue skips
a blocked ticket rather than stalling on it, and every surface that can start a
build refuses one with its reasons named.

The edges are checked here too: a ticket with no dependencies behaves exactly as
it did before, and a dependency that would close a cycle is refused rather than
stored (a cycle is a queue that can never start).
"""
from __future__ import annotations

import json

import pytest

from eng_crew import dispatch, planning, tickets, tracker
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "deps.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return str(root)


def make_ticket(project="/proj", *, criteria="- it works", priority=50,
                title="do a thing", depends_on=None) -> int:
    return tracker.add_backlog_item(
        title, "the details", project, "", priority=priority,
        acceptance_criteria=criteria, depends_on=depends_on,
    )


def ready_ticket(project="/proj", **kw) -> int:
    ticket_id = make_ticket(project, **kw)
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id


def finish(ticket_id: int, final: str = tickets.DONE) -> None:
    """Walk a ready ticket all the way to done (or on to shipped)."""
    for status in (tickets.IN_PROGRESS, tickets.IN_REVIEW, tickets.IN_QA,
                   tickets.DONE, tickets.SHIPPED):
        tickets.move(ticket_id, status, "tester")
        if status == final:
            return


# --- storage ------------------------------------------------------------


def test_a_ticket_stores_the_tickets_it_depends_on(db):
    first = make_ticket()
    second = make_ticket()
    dependent = make_ticket(depends_on=[first, second])

    assert tickets.dependencies(tracker.get_backlog_item(dependent)) == [first, second]


def test_a_ticket_with_no_dependencies_is_unchanged(db):
    """Every existing ticket has none, so none must mean "nothing to wait for"."""
    ticket_id = make_ticket()

    assert tickets.dependencies(tracker.get_backlog_item(ticket_id)) == []
    assert tickets.unmet_prerequisites(ticket_id) == []
    assert not tickets.is_blocked(ticket_id)
    tickets.move(ticket_id, tickets.READY, "tester")
    assert tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")["status"] == (
        tickets.IN_PROGRESS
    )


def test_dependencies_can_be_set_after_the_fact(db):
    first = make_ticket()
    second = make_ticket()

    assert tickets.set_dependencies(second, [first], "tester") == [first]
    assert tickets.dependencies(tracker.get_backlog_item(second)) == [first]


# --- the planner fills them in ------------------------------------------


def test_the_planner_saves_the_dependencies_between_the_tickets_it_creates(
    db, project, cfg, monkeypatch
):
    plan = {
        "tickets": [
            {"title": "Schema", "description": "the tables",
             "acceptance_criteria": "- tables exist", "priority": 10},
            {"title": "API", "description": "the endpoints",
             "acceptance_criteria": "- endpoints respond", "priority": 20,
             "depends_on": [1]},
            {"title": "UI", "description": "the screens",
             "acceptance_criteria": "- screens render", "priority": 30,
             "depends_on": [1, 2]},
        ]
    }

    class FakePlanner:
        def __init__(self, settings=None):
            pass

        def call(self, prompt, role=None, **kw):
            return LLMResult(text=json.dumps(plan))

    monkeypatch.setattr(planning, "BaseAgent", FakePlanner)

    created = planning.plan_tickets("ship v2", project, settings=cfg)

    schema, api, ui = (tracker.get_backlog_item(t["id"]) for t in created)
    assert tickets.dependencies(schema) == []
    assert tickets.dependencies(api) == [schema["id"]]
    assert tickets.dependencies(ui) == [schema["id"], api["id"]]


# --- when a prerequisite counts as met ----------------------------------


def test_a_prerequisite_is_met_once_it_is_done_or_shipped(db):
    first = ready_ticket(title="first")
    second = ready_ticket(title="second")
    dependent = make_ticket(depends_on=[first, second])

    assert [t["id"] for t in tickets.unmet_prerequisites(dependent)] == [first, second]

    finish(first, tickets.DONE)
    assert [t["id"] for t in tickets.unmet_prerequisites(dependent)] == [second]

    finish(second, tickets.SHIPPED)
    assert tickets.unmet_prerequisites(dependent) == []
    assert not tickets.is_blocked(dependent)


def test_a_rejected_prerequisite_is_not_met(db):
    first = ready_ticket(title="first")
    tickets.move(first, tickets.IN_PROGRESS, "tester")
    tickets.move(first, tickets.REJECTED, "tester")
    dependent = make_ticket(depends_on=[first])

    assert tickets.is_blocked(dependent)


# --- the queue ----------------------------------------------------------


def test_the_queue_skips_blocked_tickets_and_picks_by_priority(db, project):
    first = ready_ticket(project, title="schema", priority=10)
    blocked = make_ticket(project, title="api", priority=20, depends_on=[first])
    tickets.move(blocked, tickets.READY, "tester")
    free = ready_ticket(project, title="docs", priority=30)

    # first is still ready and highest priority, so it leads; the blocked one is
    # passed over entirely rather than sitting at the head of the queue.
    assert [t["id"] for t in tickets.queue(project)] == [first, free]

    finish(first, tickets.DONE)
    assert [t["id"] for t in tickets.queue(project)] == [blocked, free]


def test_the_queue_can_be_asked_for_blocked_tickets_too(db, project):
    first = ready_ticket(project, title="schema", priority=10)
    blocked = make_ticket(project, title="api", priority=20, depends_on=[first])
    tickets.move(blocked, tickets.READY, "tester")

    assert [t["id"] for t in tickets.queue(project, include_blocked=True)] == [
        first, blocked,
    ]


# --- refusing to build --------------------------------------------------


def test_the_lifecycle_refuses_a_build_with_unmet_prerequisites(db, cfg):
    first = ready_ticket(title="schema")
    blocked = ready_ticket(title="api", depends_on=[first])

    with pytest.raises(tickets.TransitionRefused) as exc:
        tickets.move(blocked, tickets.IN_PROGRESS, "tester", settings=cfg)

    assert f"#{first}" in str(exc.value)
    assert tracker.get_backlog_item(blocked)["status"] == tickets.READY


def test_the_cli_refuses_to_build_a_blocked_ticket(db, cfg, monkeypatch):
    """`eng-crew ticket <id>` goes through run_ticket, which must not start."""
    from eng_crew import run as run_mod

    first = ready_ticket(title="schema")
    blocked = ready_ticket(title="api", depends_on=[first])

    def no_pipeline(**kw):
        raise AssertionError("a blocked ticket must not reach the pipeline")

    monkeypatch.setattr(run_mod, "run_pipeline", no_pipeline)

    with pytest.raises(tickets.TransitionRefused) as exc:
        run_mod.run_ticket(blocked, actor="cli", settings=cfg)

    assert f"#{first}" in str(exc.value)
    assert tracker.get_backlog_item(blocked)["status"] == tickets.READY


def test_discord_build_refuses_a_blocked_ticket_and_leaves_it_alone(
    db, project, cfg, monkeypatch
):
    """`/build <id>` goes through dispatch.build_ticket. Nothing is launched."""
    monkeypatch.setattr(
        dispatch.subprocess, "Popen",
        lambda *a, **kw: pytest.fail("a blocked ticket must not be launched"),
    )
    first = ready_ticket(project, title="schema")
    blocked = make_ticket(project, title="api", depends_on=[first])

    result = dispatch.build_ticket(blocked, actor="alice", settings=cfg)

    assert not result.ok
    assert f"#{first}" in result.detail
    # Still in the backlog: the refusal happens before the approval to ready.
    assert tracker.get_backlog_item(blocked)["status"] == tickets.BACKLOG


def test_the_dashboard_refuses_to_start_a_blocked_ticket(db, project, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    first = ready_ticket(project, title="schema")
    blocked = ready_ticket(project, title="api", depends_on=[first])

    with TestClient(app) as client:
        resp = client.put(
            f"/api/tickets/{blocked}/status",
            json={"status": tickets.IN_PROGRESS, "actor": "alice"},
        )

    # 409 is what the dashboard already answers a refused move with.
    assert resp.status_code == 409
    assert f"#{first}" in resp.json()["error"]
    assert tracker.get_backlog_item(blocked)["status"] == tickets.READY


# --- showing it ---------------------------------------------------------


def test_the_surfaces_show_prerequisites_and_whether_a_ticket_is_blocked(db, project):
    discord_bot = pytest.importorskip(
        "eng_crew.discord_bot",
        reason="discord.py not installed (optional [discord] extra)",
    )
    first = ready_ticket(project, title="schema")
    blocked = ready_ticket(project, title="api", depends_on=[first])
    cfg = {"name": "proj", "path": project}

    queue_text = "\n".join(discord_bot.queue_lines(cfg))
    _, backlog_text = discord_bot.render_backlog(cfg)
    _, ticket_text = discord_bot.render_ticket(blocked)

    for text in (queue_text, backlog_text, ticket_text):
        assert f"#{first}" in text
        assert "blocked" in text.lower()


def test_the_dashboard_reports_a_tickets_prerequisites(db, project):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    first = ready_ticket(project, title="schema")
    blocked = ready_ticket(project, title="api", depends_on=[first])

    with TestClient(app) as client:
        body = client.get(f"/api/tickets/{blocked}").json()

    assert body["ticket"]["depends_on"] == [first]
    assert body["blocked"] is True
    assert [p["id"] for p in body["unmet_prerequisites"]] == [first]


# --- cycles -------------------------------------------------------------


def test_a_dependency_that_would_close_a_cycle_is_refused(db):
    first = make_ticket(title="first")
    second = make_ticket(title="second", depends_on=[first])
    third = make_ticket(title="third", depends_on=[second])

    with pytest.raises(tickets.TransitionRefused, match="cycle"):
        tickets.set_dependencies(first, [third], "tester")

    assert tickets.dependencies(tracker.get_backlog_item(first)) == []


def test_a_ticket_cannot_depend_on_itself(db):
    ticket_id = make_ticket()

    with pytest.raises(tickets.TransitionRefused):
        tickets.set_dependencies(ticket_id, [ticket_id], "tester")

    assert tickets.dependencies(tracker.get_backlog_item(ticket_id)) == []


def test_a_dependency_on_a_ticket_that_does_not_exist_is_refused(db):
    ticket_id = make_ticket()

    with pytest.raises(tickets.TransitionRefused, match="no ticket"):
        tickets.set_dependencies(ticket_id, [9999], "tester")
