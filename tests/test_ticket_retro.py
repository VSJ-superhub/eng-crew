"""The retro step — what a shipped ticket leaves behind for the next one.

A ticket's history used to be write-only: runs, spend, review verdicts and
retries were all recorded and then read by nobody, so every ticket started from
the same blank slate. These tests pin down both halves of the loop — that
shipping writes a retro from the ticket's own record, and that a later build
actually gets the relevant ones in front of it.
"""
from __future__ import annotations

import pytest

from eng_crew import retro, review, tickets, tracker
from eng_crew.agents.single_agent import SingleAgentEngineer
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "retro.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


def ticket_in_progress(project="/proj", title="do a thing", **kw) -> int:
    ticket_id = tracker.add_backlog_item(
        title, "the details", project, "", acceptance_criteria="- it works", **kw,
    )
    tickets.move(ticket_id, tickets.READY, "tester")
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester")
    return ticket_id


def build(ticket_id: int, cost: float = 1.0, status: str = "completed",
          project="/proj", branch: str = "") -> int:
    run_id = tracker.create_run("build it", project, ticket_id=ticket_id)
    if branch:
        tracker.update_run_branch(run_id, branch)
    tracker.log_event(run_id, 0, "single_agent", LLMResult(text="", cost_usd=cost))
    tracker.finish_run(run_id, status=status)
    return run_id


def shipped_ticket(project="/proj", title="do a thing", **kw) -> int:
    """A ticket that went the whole way: build, review, QA, ship."""
    ticket_id = ticket_in_progress(project, title, **kw)
    build(ticket_id, project=project)
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run finished as completed")
    tickets.note(ticket_id, "reviewer", "review: approve — criteria met")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved — criteria met")
    tickets.move(ticket_id, tickets.DONE, "qa", "verification gate passed")
    tickets.move(ticket_id, tickets.SHIPPED, "alice", "released")
    return ticket_id


def retros(project="/proj") -> list[dict]:
    return [m for m in tracker.get_memories(project, limit_project=50, limit_global=0)
            if retro.is_retro(m)]


# --- criterion 1: shipping writes a retro from the ticket's own history ---


def test_shipping_writes_a_retro_into_the_lessons_store(db):
    ticket_id = shipped_ticket(title="add pagination to the runs table")

    stored = retros()
    assert len(stored) == 1, "shipping a ticket should leave exactly one retro"
    note = stored[0]
    assert "add pagination to the runs table" in note["title"]
    assert note["project_path"] == "/proj"
    assert note["lesson"].strip(), "a retro with no lesson teaches nothing"
    assert str(ticket_id) in note["raw_text"]


def test_the_retro_records_runs_cost_against_budget_review_and_qa(db):
    ticket_id = ticket_in_progress(budget_usd=10.0)
    build(ticket_id, cost=3.0)
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run finished as completed")
    tickets.note(ticket_id, "reviewer", "review: approve — criteria met")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved — criteria met")
    tickets.move(ticket_id, tickets.DONE, "qa", "verification gate passed")

    facts = retro.history(ticket_id)
    assert facts["builds"] == 1
    assert facts["spent_usd"] == pytest.approx(3.0)
    assert facts["budget_usd"] == pytest.approx(10.0)
    assert [v for v, _ in facts["verdicts"]] == [review.APPROVE], \
        "an approval recorded as both a note and a move is one verdict, not two"
    assert facts["qa_passed"] is True

    tickets.move(ticket_id, tickets.SHIPPED, "alice", "released")
    body = retros()[0]["raw_text"]
    assert "$3.00 of $10.00" in body
    assert "qa: passed" in body


def test_the_retro_records_retries_and_what_the_reviewer_objected_to(db):
    ticket_id = ticket_in_progress()
    # First attempt: built, reviewed, sent back.
    build(ticket_id, cost=2.0)
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run finished as completed")
    tickets.note(ticket_id, "reviewer",
                 "review: changes_requested — no test covers the second criterion")
    # Second attempt: rebuilt and approved.
    tickets.move(ticket_id, tickets.IN_PROGRESS, "alice", "rebuild")
    build(ticket_id, cost=2.0)
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run finished as completed")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved — criteria met")
    tickets.move(ticket_id, tickets.DONE, "qa", "verification gate passed")
    tickets.move(ticket_id, tickets.SHIPPED, "alice", "released")

    note = retros()[0]
    assert "2 builds" in note["what_didnt"]
    assert "no test covers the second criterion" in note["what_didnt"]
    assert "no test covers the second criterion" in note["lesson"]


def test_the_retro_is_credited_to_retro_which_cannot_ship_anything(db):
    ticket_id = shipped_ticket()
    last = tickets.events(ticket_id)[-1]
    assert last["actor"] == retro.ACTOR
    assert last["note"].startswith(retro.NOTE_PREFIX)

    assert retro.ACTOR in tickets.ROBOTS
    other = ticket_in_progress()
    tickets.move(other, tickets.IN_REVIEW, "queue", "landed")
    tickets.move(other, tickets.IN_QA, "reviewer", "review: approved")
    tickets.move(other, tickets.DONE, "qa", "gate passed")
    with pytest.raises(tickets.TransitionRefused):
        tickets.move(other, tickets.SHIPPED, retro.ACTOR, "ship it")


def test_the_retro_costs_nothing_and_never_blocks_the_ship(db, monkeypatch):
    # conftest's guard already fails any real model call; this pins the intent.
    ticket_id = ticket_in_progress()
    build(ticket_id)
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "landed")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved")
    tickets.move(ticket_id, tickets.DONE, "qa", "gate passed")
    spent_before = tracker.ticket_spend(ticket_id)

    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store is down"))
    monkeypatch.setattr(tracker, "add_memory", boom)

    ticket = tickets.move(ticket_id, tickets.SHIPPED, "alice", "released")
    assert ticket["status"] == tickets.SHIPPED, "a broken retro must not unship a ticket"
    assert tracker.ticket_spend(ticket_id) == pytest.approx(spent_before)


# --- criterion 2: the lessons reach the builder of a later ticket ---


def test_relevant_retros_are_selected_for_a_new_task(db):
    shipped_ticket(title="add pagination to the runs table")
    shipped_ticket(title="rewrite the discord bot reconnect loop")
    shipped_ticket(title="paginate the tickets table in the dashboard")

    picked = retro.lessons_for("/proj", "add pagination to the tickets list", limit=2)
    titles = " ".join(m["title"] for m in picked)
    assert "pagination" in titles or "paginate" in titles
    assert "discord" not in titles, "an unrelated retro should rank below relevant ones"

    block = retro.lessons_block("/proj", "add pagination to the tickets list", limit=2)
    assert "LESSONS FROM PAST TICKETS" in block
    assert "discord" not in block


def test_no_retros_means_no_block_at_all(db):
    assert retro.lessons_for("/proj", "anything") == []
    assert retro.lessons_block("/proj", "anything") == ""


def test_the_builder_is_given_the_past_retros(db, monkeypatch):
    prompts_seen: list[str] = []

    def fake_call(provider, model, prompt, **kwargs):
        prompts_seen.append(prompt)
        return LLMResult(text="done")

    monkeypatch.setattr("eng_crew.agents.single_agent.call_llm", fake_call)

    agent = SingleAgentEngineer(Settings())
    state = {
        "raw_task": "add pagination to the tickets table",
        "project_path": ".",
        "run_id": 0,
        "project_context": "",
        "retro_lessons": "=== LESSONS FROM PAST TICKETS ===\n- Retro — paginate: keep it tight",
    }
    agent.run(state)

    assert prompts_seen, "the builder was never called"
    assert "LESSONS FROM PAST TICKETS" in prompts_seen[0]
    assert "keep it tight" in prompts_seen[0]


def test_a_build_with_no_lessons_gets_an_unchanged_prompt(db, monkeypatch):
    prompts_seen: list[str] = []
    monkeypatch.setattr(
        "eng_crew.agents.single_agent.call_llm",
        lambda p, m, prompt, **k: (prompts_seen.append(prompt), LLMResult(text="done"))[1],
    )
    agent = SingleAgentEngineer(Settings())
    agent.run({"raw_task": "do a thing", "project_path": ".", "run_id": 0,
               "project_context": "", "retro_lessons": ""})
    assert "LESSONS FROM PAST TICKETS" not in prompts_seen[0]
    assert "{lessons_block}" not in prompts_seen[0], "the placeholder must be filled"
