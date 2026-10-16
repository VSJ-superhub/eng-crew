"""eng-crew's own health: are its runs landing, is the reviewer approving,
how many builds does a ticket take, and what does one cost in money and time?

Every number here is read back off records the pipeline already keeps — runs,
backlog_items, ticket_events, subtask_events and the new tool_call_events — so
the metrics cannot drift from what actually happened. These tests fix the
arithmetic (what counts as a success, which review event is the verdict and
which is the status move that repeats it) and the JSON the dashboard reads.
"""
from __future__ import annotations

import pytest

from eng_crew import tracker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "health.db")
    tracker.close_connection()
    tracker._init_db()
    yield tracker
    tracker.close_connection()


def _ticket(title: str, project: str = "/proj") -> int:
    return tracker.add_backlog_item(title, "desc", project, "CLAUDE.md",
                                    acceptance_criteria="- done")


def _run(ticket_id: int | None, status: str, *, cost: float = 0.0,
         started: str = "2026-10-01T00:00:00Z",
         finished: str | None = "2026-10-01T00:05:00Z") -> int:
    conn = tracker._connect()
    with conn:
        return conn.execute(
            """INSERT INTO runs (task_text, project_path, started_at, finished_at,
                                 status, total_cost_usd, ticket_id)
               VALUES (?,?,?,?,?,?,?)""",
            ("t", "/proj", started, finished, status, cost, ticket_id),
        ).lastrowid


def _verdict(ticket_id: int, verdict: str) -> None:
    tracker.add_ticket_event(ticket_id, "in_review", "in_review", "reviewer",
                             f"review: {verdict} — because")


# ── Run success rate ─────────────────────────────────────────────────────────

def test_run_success_rate_counts_completed_against_failed_and_unverified(db):
    _run(None, "completed")
    _run(None, "completed")
    _run(None, "failed")
    _run(None, "unverified")
    _run(None, "running")  # still in flight — not yet an outcome

    stats = tracker.get_run_success_rate()
    assert stats["completed"] == 2
    assert stats["failed"] == 1
    assert stats["unverified"] == 1
    assert stats["finished"] == 4
    assert stats["rate"] == pytest.approx(0.5)


def test_run_success_rate_is_defined_with_no_runs_at_all(db):
    stats = tracker.get_run_success_rate()
    assert stats["finished"] == 0
    assert stats["rate"] == 0.0


# ── Review approval rate ─────────────────────────────────────────────────────

def test_review_approval_rate_reads_the_reviewer_verdicts(db):
    a, b, c = _ticket("a"), _ticket("b"), _ticket("c")
    _verdict(a, "approve")
    _verdict(b, "approve")
    _verdict(c, "changes_requested")

    stats = tracker.get_review_approval_rate()
    assert stats["approve"] == 2
    assert stats["changes_requested"] == 1
    assert stats["rate"] == pytest.approx(2 / 3)


def test_an_approval_is_not_counted_twice_by_its_status_move(db):
    """The reviewer writes the verdict as a note and again as the reason for the
    in_review -> in_qa move. One review, one verdict."""
    ticket_id = _ticket("a")
    _verdict(ticket_id, "approve")
    tracker.add_ticket_event(ticket_id, "in_review", "in_qa", "reviewer",
                             "review: approved — because")

    assert tracker.get_review_approval_rate()["approve"] == 1


def test_an_unavailable_review_is_not_a_verdict(db):
    ticket_id = _ticket("a")
    tracker.add_ticket_event(ticket_id, "in_review", "in_review", "reviewer",
                             "review unavailable: no API key")

    stats = tracker.get_review_approval_rate()
    assert stats["approve"] == 0 and stats["changes_requested"] == 0
    assert stats["rate"] == 0.0


# ── Builds per ticket ────────────────────────────────────────────────────────

def test_average_builds_per_ticket_counts_every_run_a_ticket_took(db):
    one, two = _ticket("one"), _ticket("two")
    _run(one, "completed")
    _run(two, "failed")
    _run(two, "completed")
    _run(None, "completed")  # a run with no ticket is not a build of one

    stats = tracker.get_avg_builds_per_ticket()
    assert stats["tickets"] == 2
    assert stats["builds"] == 3
    assert stats["average"] == pytest.approx(1.5)


def test_average_builds_per_ticket_is_defined_with_no_tickets(db):
    assert tracker.get_avg_builds_per_ticket()["average"] == 0.0


# ── Cost and duration per ticket, over time ──────────────────────────────────

def test_cost_and_duration_series_totals_every_run_of_a_ticket(db):
    ticket_id = _ticket("a")
    _run(ticket_id, "failed", cost=1.5,
         started="2026-10-01T00:00:00Z", finished="2026-10-01T00:01:00Z")
    _run(ticket_id, "completed", cost=2.5,
         started="2026-10-01T01:00:00Z", finished="2026-10-01T01:02:00Z")

    [point] = tracker.get_ticket_cost_duration_series()
    assert point["ticket_id"] == ticket_id
    assert point["title"] == "a"
    assert point["cost_usd"] == pytest.approx(4.0)
    assert point["duration_secs"] == pytest.approx(180.0)
    assert point["builds"] == 2
    assert point["started_at"] == "2026-10-01T00:00:00Z"


def test_the_series_is_ordered_oldest_first_and_skips_ticketless_runs(db):
    first, second = _ticket("first"), _ticket("second")
    _run(second, "completed", started="2026-10-02T00:00:00Z",
         finished="2026-10-02T00:01:00Z")
    _run(first, "completed", started="2026-10-01T00:00:00Z",
         finished="2026-10-01T00:01:00Z")
    _run(None, "completed")

    series = tracker.get_ticket_cost_duration_series()
    assert [p["title"] for p in series] == ["first", "second"]


# ── Tool calls feed the same view ────────────────────────────────────────────

def test_tool_call_stats_summarise_the_traced_calls(db):
    run_id = _run(None, "completed")
    for name, outcome in [("Read", "success"), ("Read", "success"),
                          ("Bash", "error")]:
        tracker.log_tool_call_event(run_id, -1, name, "2026-10-01T00:00:00Z", 10,
                                    outcome)

    stats = tracker.get_tool_call_stats()
    assert stats["total"] == 3
    assert stats["errors"] == 1
    assert {t["tool_name"]: t["calls"] for t in stats["by_tool"]} == {"Read": 2, "Bash": 1}


# ── The endpoint the dashboard reads ─────────────────────────────────────────

def test_health_endpoint_exposes_all_four_metrics(db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from eng_crew.dashboard.app import app

    ticket_id = _ticket("a")
    _run(ticket_id, "completed", cost=3.0)
    _run(None, "failed")
    _verdict(ticket_id, "approve")

    with TestClient(app) as client:
        body = client.get("/api/health/metrics").json()

    assert body["run_success"]["rate"] == pytest.approx(0.5)
    assert body["review_approval"]["approve"] == 1
    assert body["builds_per_ticket"]["average"] == pytest.approx(1.0)
    assert [p["ticket_id"] for p in body["ticket_series"]] == [ticket_id]
    assert body["ticket_series"][0]["cost_usd"] == pytest.approx(3.0)
    assert "tool_calls" in body


def test_health_page_is_reachable_from_the_dashboard_nav():
    """No JS test runner here, so this pins the source contract: a Health page
    exists, is routed, and is linked from the nav."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "eng_crew/dashboard/frontend/src"
    assert (root / "pages/Health.tsx").exists()
    assert "Health" in (root / "App.tsx").read_text(encoding="utf-8")
    assert "Health" in (root / "components/Layout.tsx").read_text(encoding="utf-8")
