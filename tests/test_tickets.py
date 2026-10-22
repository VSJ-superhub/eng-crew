"""The ticket lifecycle — eng-crew's unit of work.

Three things here are load-bearing rather than decorative:

  - a ticket with no acceptance criteria cannot become `ready`, because nothing
    downstream could tell whether it was finished;
  - the WIP limit is the only backpressure on a continuous queue, so it has to
    actually refuse work rather than warn about it;
  - a run is told what the *ticket* says. The bug this replaced launched builds
    against the literal string "Execute sprint N", which is why the text handed
    to the pipeline is asserted directly.
"""
from __future__ import annotations

import pytest

from eng_crew import tickets, tracker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "tickets.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


def make_ticket(project="/proj", *, criteria="", priority=50, title="do a thing") -> int:
    return tracker.add_backlog_item(
        title, "the details", project, "", priority=priority,
        acceptance_criteria=criteria,
    )


def ready_ticket(project="/proj", **kw) -> int:
    ticket_id = make_ticket(project, criteria="- it works", **kw)
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id


# --- the acceptance criteria gate ---------------------------------------


def test_a_new_ticket_starts_in_the_backlog(db):
    ticket_id = make_ticket()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG


def test_a_ticket_without_acceptance_criteria_cannot_become_ready(db):
    ticket_id = make_ticket()
    with pytest.raises(tickets.TransitionRefused, match="acceptance criteria"):
        tickets.move(ticket_id, tickets.READY, "tester")
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG


def test_whitespace_is_not_acceptance_criteria(db):
    ticket_id = make_ticket(criteria="   \n  ")
    with pytest.raises(tickets.TransitionRefused, match="acceptance criteria"):
        tickets.move(ticket_id, tickets.READY, "tester")


def test_adding_criteria_unblocks_ready(db):
    ticket_id = make_ticket()
    tracker.update_backlog_item(ticket_id, acceptance_criteria="- the thing works")
    assert tickets.move(ticket_id, tickets.READY, "tester")["status"] == tickets.READY


# --- the lifecycle ------------------------------------------------------


def test_the_whole_lifecycle_runs_end_to_end(db):
    ticket_id = ready_ticket()
    for status in (tickets.IN_PROGRESS, tickets.IN_REVIEW, tickets.IN_QA,
                   tickets.DONE, tickets.SHIPPED):
        assert tickets.move(ticket_id, status, "tester")["status"] == status


def test_qa_can_reject_instead_of_finishing(db):
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    tickets.move(ticket_id, tickets.IN_REVIEW, "tester")
    tickets.move(ticket_id, tickets.IN_QA, "tester")
    assert tickets.move(ticket_id, tickets.REJECTED, "qa")["status"] == tickets.REJECTED
    # A rejected ticket goes back to the queue rather than dying.
    assert tickets.move(ticket_id, tickets.READY, "tester")["status"] == tickets.READY


def test_a_ticket_cannot_skip_the_middle_of_the_lifecycle(db):
    ticket_id = ready_ticket()
    with pytest.raises(tickets.TransitionRefused, match="can only move to"):
        tickets.move(ticket_id, tickets.DONE, "tester")


def test_a_shipped_ticket_is_the_end_of_the_line(db):
    ticket_id = ready_ticket()
    for status in (tickets.IN_PROGRESS, tickets.IN_REVIEW, tickets.IN_QA,
                   tickets.DONE, tickets.SHIPPED):
        tickets.move(ticket_id, status, "tester")
    with pytest.raises(tickets.TransitionRefused):
        tickets.move(ticket_id, tickets.BACKLOG, "tester")


def test_moving_a_ticket_to_where_it_already_is_is_refused(db):
    ticket_id = make_ticket()
    with pytest.raises(tickets.TransitionRefused, match="already"):
        tickets.move(ticket_id, tickets.BACKLOG, "tester")


def test_an_unknown_status_is_refused(db):
    ticket_id = make_ticket()
    with pytest.raises(tickets.TransitionRefused, match="not a ticket status"):
        tickets.move(ticket_id, "almost-done", "tester")


def test_a_move_lost_to_a_concurrent_writer_is_refused(db, monkeypatch):
    """The compare-and-set is what keeps two dashboards from both claiming the
    same ticket; a lost race must raise rather than report success."""
    ticket_id = ready_ticket()
    monkeypatch.setattr(tracker, "set_ticket_status", lambda *a, **kw: False)
    with pytest.raises(tickets.TransitionRefused, match="underneath us"):
        tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")


# --- events -------------------------------------------------------------


def test_creating_a_ticket_is_itself_an_event(db):
    ticket_id = make_ticket()
    events = tickets.events(ticket_id)
    assert [(e["from_status"], e["to_status"]) for e in events] == [("", tickets.BACKLOG)]


def test_every_status_change_records_who_made_it(db):
    ticket_id = make_ticket(criteria="- it works")
    tickets.move(ticket_id, tickets.READY, "alex", "groomed")
    tickets.move(ticket_id, tickets.IN_PROGRESS, "the queue")

    events = tickets.events(ticket_id)
    assert [e["actor"] for e in events[1:]] == ["alex", "the queue"]
    assert events[1]["note"] == "groomed"
    assert [(e["from_status"], e["to_status"]) for e in events[1:]] == [
        (tickets.BACKLOG, tickets.READY),
        (tickets.READY, tickets.IN_PROGRESS),
    ]


def test_a_refused_move_records_nothing(db):
    ticket_id = make_ticket()
    with pytest.raises(tickets.TransitionRefused):
        tickets.move(ticket_id, tickets.READY, "tester")
    assert len(tickets.events(ticket_id)) == 1, "a refused move left a trace"


# --- the queue and its WIP limit ----------------------------------------


def test_the_queue_holds_ready_tickets_in_priority_order(db):
    low = ready_ticket(priority=90, title="later")
    high = ready_ticket(priority=10, title="sooner")
    make_ticket(criteria="- x", title="not ready yet")

    assert [t["id"] for t in tickets.queue("/proj")] == [high, low]


def test_the_queue_is_per_project(db):
    mine = ready_ticket("/proj")
    ready_ticket("/other")
    assert [t["id"] for t in tickets.queue("/proj")] == [mine]


def test_subtasks_are_not_tickets(db):
    """Subtask rows share the table but are run bookkeeping, not work items —
    they must not show up in the queue or count against the WIP limit."""
    tracker.create_subtask_backlog_items(
        run_id=1,
        subtasks=[{"id": "s1", "description": "edit a file", "agent_type": "backend"}],
        project_path="/proj", claude_md_path="", project_id=None,
    )
    assert tickets.queue("/proj") == []
    assert tracker.count_tickets_in_flight("/proj") == 0
    assert tracker.list_project_tickets("/proj") == []


def test_in_flight_tickets_consume_wip_slots(db):
    for _ in range(2):
        ticket_id = ready_ticket()
        tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")

    state = tickets.wip("/proj")
    assert state["in_flight"] == 2
    assert state["slots"] == state["limit"] - 2


def test_review_and_qa_still_hold_a_slot(db):
    """A ticket waiting on a human is still work in progress. If review did not
    count, the limit would only bound coding and the queue would flood review."""
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    tickets.move(ticket_id, tickets.IN_REVIEW, "tester")
    assert tickets.wip("/proj")["in_flight"] == 1

    tickets.move(ticket_id, tickets.IN_QA, "tester")
    assert tickets.wip("/proj")["in_flight"] == 1


def test_nothing_starts_past_the_wip_limit(db, monkeypatch):
    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 2)
    started = []
    for _ in range(2):
        ticket_id = ready_ticket()
        tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
        started.append(ticket_id)

    blocked = ready_ticket()
    with pytest.raises(tickets.TransitionRefused, match="WIP limit"):
        tickets.move(blocked, tickets.IN_PROGRESS, "tester")

    # Finishing one frees the slot.
    tickets.move(started[0], tickets.IN_REVIEW, "tester")
    tickets.move(started[0], tickets.IN_QA, "tester")
    tickets.move(started[0], tickets.DONE, "tester")
    assert tickets.move(blocked, tickets.IN_PROGRESS, "tester")["status"] == tickets.IN_PROGRESS


def test_the_wip_limit_is_per_project(db, monkeypatch):
    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 1)
    mine = ready_ticket("/proj")
    tickets.move(mine, tickets.IN_PROGRESS, "tester")

    theirs = ready_ticket("/other")
    assert tickets.move(theirs, tickets.IN_PROGRESS, "tester")["status"] == tickets.IN_PROGRESS


def test_a_ticket_already_in_flight_does_not_re_spend_its_slot(db, monkeypatch):
    """in_review -> in_progress is a rework loop, not a new claim. At the limit
    it must still be allowed, or rework would deadlock."""
    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 1)
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    tickets.move(ticket_id, tickets.IN_REVIEW, "reviewer")

    assert tickets.move(ticket_id, tickets.IN_PROGRESS, "reviewer")["status"] == tickets.IN_PROGRESS


# --- what a run is actually told to do ----------------------------------


def test_the_task_text_is_the_ticket(db):
    ticket = {
        "title": "Add pagination",
        "description": "20 rows per page on the users table",
        "acceptance_criteria": "- page 2 shows rows 21-40",
    }
    text = tickets.task_text(ticket)
    assert "Add pagination" in text
    assert "20 rows per page" in text
    assert "page 2 shows rows 21-40" in text


def test_the_task_text_does_not_repeat_a_description_that_is_the_title(db):
    text = tickets.task_text({"title": "Fix the login bug", "description": "Fix the login bug"})
    assert text.count("Fix the login bug") == 1


def test_running_a_ticket_hands_the_pipeline_the_tickets_own_text(db, monkeypatch):
    """The regression this guards: queued work used to be launched as the literal
    string "Execute sprint N", so the agent never saw the actual ticket."""
    from eng_crew import run as run_mod

    ticket_id = ready_ticket(title="Add pagination")
    seen = {}

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        seen.update(task=task, project_path=project_path, run_id=run_id)
        tracker.finish_run(run_id, status="completed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    run_mod.run_ticket(ticket_id, actor="tester")

    assert "Add pagination" in seen["task"]
    assert "it works" in seen["task"], "the acceptance criteria were not passed on"
    assert "sprint" not in seen["task"].lower()


def test_a_run_is_linked_to_the_ticket_it_attempts(db, monkeypatch):
    from eng_crew import run as run_mod

    ticket_id = ready_ticket()
    run_ids = []

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        run_ids.append(run_id)
        tracker.finish_run(run_id, status="completed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    run_mod.run_ticket(ticket_id, actor="tester")

    linked = tracker.get_ticket_run(ticket_id)
    assert linked["id"] == run_ids[0]
    assert linked["ticket_id"] == ticket_id


def test_a_finished_run_leaves_the_ticket_awaiting_review(db, monkeypatch):
    from eng_crew import run as run_mod

    ticket_id = ready_ticket()

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        tracker.finish_run(run_id, status="completed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    run_mod.run_ticket(ticket_id, actor="tester")

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW


def test_a_failed_run_rejects_the_ticket(db, monkeypatch):
    from eng_crew import run as run_mod

    ticket_id = ready_ticket()

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        tracker.finish_run(run_id, status="failed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    run_mod.run_ticket(ticket_id, actor="tester")

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED


def test_a_pipeline_that_raises_still_releases_the_ticket(db, monkeypatch):
    """A ticket stuck in in_progress holds a WIP slot forever, so a crash must
    not leave one behind."""
    from eng_crew import run as run_mod

    ticket_id = ready_ticket()

    def boom(*, task, project_path, settings, run_id, base_branch=None):
        raise RuntimeError("the provider died")

    monkeypatch.setattr(run_mod, "run_pipeline", boom)
    with pytest.raises(RuntimeError):
        run_mod.run_ticket(ticket_id, actor="tester")

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED
    assert tickets.wip("/proj")["in_flight"] == 0


def test_running_a_ticket_over_the_wip_limit_starts_nothing(db, monkeypatch):
    from eng_crew import run as run_mod

    monkeypatch.setattr(tickets, "wip_limit", lambda settings=None: 1)
    held = ready_ticket()
    tickets.move(held, tickets.IN_PROGRESS, "tester")

    blocked = ready_ticket()
    monkeypatch.setattr(run_mod, "run_pipeline",
                        lambda **kw: pytest.fail("a run started past the WIP limit"))
    with pytest.raises(tickets.TransitionRefused, match="WIP limit"):
        run_mod.run_ticket(blocked, actor="tester")


# --- cleanup after an interrupted run -----------------------------------


def test_cleanup_moves_a_landed_ticket_to_review(db):
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    run_id = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=run_id)
    tracker.finish_run(run_id, status="completed")

    assert tracker.cleanup_stale_backlog_items() == 1
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    # The rewrite is recorded like any other status change.
    assert tickets.events(ticket_id)[-1]["actor"] == "cleanup"


def test_cleanup_rejects_a_ticket_whose_run_failed(db):
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    run_id = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=run_id)
    tracker.finish_run(run_id, status="failed")

    tracker.cleanup_stale_backlog_items()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED


def _age_ticket_events(ticket_id: int, minutes: int) -> None:
    with tracker._connect() as conn:
        conn.execute(
            "UPDATE ticket_events SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
            " WHERE ticket_id = ?",
            (f"-{minutes} minutes", ticket_id),
        )


def test_cleanup_returns_a_ticket_with_no_run_to_the_backlog(db):
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    _age_ticket_events(ticket_id, tracker.ORPHAN_CLAIM_GRACE_MINUTES + 1)

    tracker.cleanup_stale_backlog_items()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG


def test_cleanup_leaves_a_just_claimed_ticket_for_its_starting_build(db):
    """A dispatch claims the ticket, then launches a build process that runs this
    cleanup on import before it has created the run. Resetting that claim made
    every dispatched build refuse to start."""
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")

    assert tracker.cleanup_stale_backlog_items() == 0
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_PROGRESS


def _rebuilt_ticket() -> tuple[int, int]:
    """A ticket whose first run failed, re-readied and claimed for a new build.

    Its run_id still points at the failed attempt: the new build process has
    not created its own run yet.
    """
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    old_run = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=old_run)
    tracker.finish_run(old_run, status="failed")
    with tracker._connect() as conn:
        conn.execute(
            "UPDATE runs SET started_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 hours')"
            " WHERE id = ?", (old_run,),
        )
        conn.execute(
            "UPDATE ticket_events SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 hours')"
            " WHERE ticket_id = ?", (ticket_id,),
        )
    tracker.cleanup_stale_backlog_items()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED
    tickets.move(ticket_id, tickets.READY, "tester")
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    return ticket_id, old_run


def test_cleanup_does_not_judge_a_rebuild_by_the_previous_runs_failure(db):
    """Rebuilding a ticket whose last run failed was rejected a second after it
    was claimed: cleanup saw in_progress plus a failed run_id, and that run was
    the previous attempt's."""
    ticket_id, _ = _rebuilt_ticket()

    assert tracker.cleanup_stale_backlog_items() == 0
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_PROGRESS


def test_a_rebuild_whose_build_never_started_is_still_an_orphan(db):
    ticket_id, _ = _rebuilt_ticket()
    _age_ticket_events(ticket_id, tracker.ORPHAN_CLAIM_GRACE_MINUTES + 1)

    tracker.cleanup_stale_backlog_items()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG


def test_a_rebuilds_own_run_is_what_cleanup_reads(db):
    ticket_id, _ = _rebuilt_ticket()
    new_run = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=new_run)
    tracker.finish_run(new_run, status="completed")

    assert tracker.cleanup_stale_backlog_items() == 1
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW


def test_cleanup_leaves_a_ticket_whose_run_is_still_going(db):
    ticket_id = ready_ticket()
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    run_id = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=run_id)

    assert tracker.cleanup_stale_backlog_items() == 0
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_PROGRESS


def test_archived_items_migrate_to_a_status_they_can_leave(db):
    """Pre-lifecycle rows marked `archived` must land on a real status, or they
    are stranded: a status outside the lifecycle has no legal moves at all."""
    ticket_id = make_ticket()
    with tracker._connect() as conn:
        conn.execute("UPDATE backlog_items SET status='archived' WHERE id=?", (ticket_id,))
        tracker._migrate_ticket_statuses(conn)

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED
    assert tickets.move(ticket_id, tickets.BACKLOG, "tester")["status"] == tickets.BACKLOG
