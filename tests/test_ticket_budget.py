"""Spend authority on a ticket — the budget that stops work instead of warning.

A ticket is the unit of work, and until now it was also an unbounded line of
credit: a run that went badly could be repaired, re-reviewed and rebuilt with
nothing counting the money. These tests pin down the company analogy the
feature is modelled on — work proceeds within budget, and crossing it stops
and escalates to a person rather than spending more.

The enforcement points matter as much as the accounting. A model call cannot be
interrupted mid-flight, so the budget is checked where a *new* paid step would
start: entering `in_progress` (a build), and before a repair pass or a review.
A step already running is allowed to finish.
"""
from __future__ import annotations

import pytest

from eng_crew import budget, tickets, tracker, verify
from eng_crew.agents.verifier import VerifierAgent
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "budget.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


def ready_ticket(project="/proj", **kw) -> int:
    ticket_id = tracker.add_backlog_item(
        "do a thing", "the details", project, "",
        acceptance_criteria="- it works", **kw,
    )
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id


def charge(ticket_id: int | None, amount: float, *, agent="single_agent",
           status: str | None = "completed", project="/proj") -> int:
    """A run against the ticket that cost `amount`. status=None: still running."""
    run_id = tracker.create_run("build it", project, ticket_id=ticket_id)
    tracker.log_event(run_id, 0, agent, LLMResult(text="", cost_usd=amount))
    if status:
        tracker.finish_run(run_id, status=status)
    return run_id


def history(ticket_id: int) -> str:
    return "\n".join(e["note"] for e in tickets.events(ticket_id))


@pytest.fixture
def failing_gate(monkeypatch):
    """The verification gate fails, and every repair pass is recorded not made."""
    def fake_verify(*a, **k):
        red = verify.CheckResult("pytest", "test", verify.FAILED,
                                 output="1 failed", cmd="pytest")
        return verify.VerificationResult(results=[red])

    monkeypatch.setattr(verify, "verify", fake_verify)
    monkeypatch.setattr(verify, "snapshot_tests", lambda *a, **k: {})
    monkeypatch.setattr(verify, "verify_red", lambda *a, **k: None)

    repairs: list[int] = []
    monkeypatch.setattr(
        VerifierAgent, "_repair",
        lambda self, state, result, run_id, attempt: (repairs.append(attempt), "")[1],
    )
    return repairs


# --- criterion 1: every ticket has a budget, configurable and overridable ---


def test_every_ticket_has_a_budget_that_defaults_and_is_overridable(db):
    plain = ready_ticket()
    assert budget.status(plain)["budget_usd"] == pytest.approx(15.0)

    configured = Settings(ticket_budget_usd=25.0)
    assert budget.status(plain, settings=configured)["budget_usd"] == pytest.approx(25.0)

    # A per-ticket budget beats the configured default, in either direction.
    rich = ready_ticket(budget_usd=40.0)
    assert budget.status(rich, settings=configured)["budget_usd"] == pytest.approx(40.0)


# --- criterion 2: spend across every run, repairs and reviews included -----


def test_a_tickets_spend_totals_every_run_charged_to_it(db):
    ticket_id = ready_ticket()
    run_id = charge(ticket_id, 4.0)
    # Both of these are charged to the run *after* it finished, so a total
    # taken from runs.total_cost_usd alone would miss them.
    tracker.log_event(run_id, -1, "verify_repair_1", LLMResult(text="", cost_usd=1.5))
    tracker.log_event(run_id, -1, "ticket_reviewer", LLMResult(text="", cost_usd=0.5))
    charge(ticket_id, 2.0)  # a second run on the same ticket

    state = budget.status(ticket_id)
    assert state["spent_usd"] == pytest.approx(8.0)
    assert state["budget_usd"] == pytest.approx(15.0)
    assert state["remaining_usd"] == pytest.approx(7.0)
    assert state["exhausted"] is False


def test_the_dashboard_shows_a_tickets_spend_alongside_its_budget(db, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    ticket_id = ready_ticket(budget_usd=9.0)
    charge(ticket_id, 2.25)

    with TestClient(app) as client:
        body = client.get(f"/api/tickets/{ticket_id}").json()

    assert body["budget"]["budget_usd"] == pytest.approx(9.0)
    assert body["budget"]["spent_usd"] == pytest.approx(2.25)


# --- criterion 3: a spent budget stops the next build and escalates --------


def test_a_build_does_not_start_once_the_budget_is_spent(db):
    ticket_id = ready_ticket(budget_usd=5.0)
    charge(ticket_id, 5.0)

    with pytest.raises(budget.BudgetExhausted):
        tickets.move(ticket_id, tickets.IN_PROGRESS, "queue", "build requested")

    # Nothing moved, so nothing retries it: it sits where a person will see it.
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY
    recorded = history(ticket_id)
    assert "budget" in recorded
    assert "human decision" in recorded


# --- criterion 4: the step in flight finishes; the next one does not start --


def test_a_repair_pass_does_not_start_once_the_build_crossed_the_budget(db, failing_gate):
    ticket_id = ready_ticket(budget_usd=3.0)
    tickets.move(ticket_id, tickets.IN_PROGRESS, "queue")
    run_id = charge(ticket_id, 3.5, status=None)  # the build that overran, still open

    out = VerifierAgent(Settings()).run(
        {"raw_task": "do it", "project_path": ".", "run_id": run_id}
    )

    assert failing_gate == []  # the build was allowed to finish; the repair never began
    assert out["verification_passed"] is False
    assert "human decision" in history(ticket_id)


def test_a_repair_pass_still_runs_while_the_ticket_is_within_budget(db, failing_gate):
    ticket_id = ready_ticket(budget_usd=50.0)
    tickets.move(ticket_id, tickets.IN_PROGRESS, "queue")
    run_id = charge(ticket_id, 3.5, status=None)

    VerifierAgent(Settings()).run(
        {"raw_task": "do it", "project_path": ".", "run_id": run_id}
    )

    assert failing_gate == [1]


# --- criterion 5: raising the budget releases the work ---------------------


def test_raising_a_tickets_budget_lets_work_on_it_continue(db):
    ticket_id = ready_ticket(budget_usd=5.0)
    charge(ticket_id, 5.0)
    with pytest.raises(budget.BudgetExhausted):
        tickets.move(ticket_id, tickets.IN_PROGRESS, "queue")

    budget.set_budget(ticket_id, 20.0, actor="alway")

    ticket = tickets.move(ticket_id, tickets.IN_PROGRESS, "queue", "build requested")
    assert ticket["status"] == tickets.IN_PROGRESS
    assert "20" in history(ticket_id)


# --- criterion 6: a run with no ticket behaves as it always did ------------


def test_a_run_with_no_ticket_is_never_stopped_for_budget(db, failing_gate):
    run_id = charge(None, 500.0, status=None)

    # There is no ticket, so there is no budget to be over — not a zero one.
    assert budget.status_for_run(run_id) is None

    VerifierAgent(Settings()).run(
        {"raw_task": "do it", "project_path": ".", "run_id": run_id}
    )

    assert failing_gate == [1]
