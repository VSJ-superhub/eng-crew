"""The Discord bot's read-only tracker views.

The tracker used to be visible only on the dashboard, so a ticket filed from a
phone through `/ideate` vanished from view. These assert the two things that
make the text view worth having rather than decorative:

  - it shows the facts that decide what to do next — WIP, status, spend against
    budget, and whether a ticket has a definition of done at all;
  - it reads the tracker and nothing else: rendering a view never moves a
    ticket or starts a run.
"""
from __future__ import annotations

import pytest

from eng_crew import discord_bot, tickets, tracker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "tickets.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


CFG = {"name": "Demo", "path": "/proj"}


def make_ticket(*, criteria="", priority=50, title="do a thing", project="/proj") -> int:
    return tracker.add_backlog_item(
        title, "the details", project, "", priority=priority,
        acceptance_criteria=criteria,
    )


def test_an_empty_tracker_says_so_and_points_somewhere(db):
    title, body = discord_bot.render_backlog(CFG)
    assert "Demo" in title
    assert "No tickets yet" in body
    assert "0/3 in flight" in body


def test_the_backlog_shows_each_ticket_with_its_status_and_spend(db):
    first = make_ticket(title="add pagination", criteria="- it paginates")
    tickets.move(first, tickets.READY, "tester")
    second = make_ticket(title="fix the footer", priority=10)

    _, body = discord_bot.render_backlog(CFG)
    assert f"#{first}" in body and "add pagination" in body
    assert f"#{second}" in body and "fix the footer" in body
    assert "ready" in body and "backlog" in body
    assert "$0.00/$15.00" in body
    # A ticket with no criteria can never be made ready, so the view says so.
    assert "no criteria" in body


def test_a_status_filter_shows_only_that_status(db):
    ready = make_ticket(title="shipped soon", criteria="- it works")
    tickets.move(ready, tickets.READY, "tester")
    make_ticket(title="not yet triaged")

    _, body = discord_bot.render_backlog(CFG, status=tickets.READY)
    assert "shipped soon" in body
    assert "not yet triaged" not in body

    _, empty = discord_bot.render_backlog(CFG, status=tickets.DONE)
    assert "No done tickets" in empty


def test_the_backlog_is_scoped_to_one_project(db):
    make_ticket(title="ours")
    make_ticket(title="theirs", project="/other")

    _, body = discord_bot.render_backlog(CFG)
    assert "ours" in body
    assert "theirs" not in body


def test_a_long_backlog_is_truncated_with_a_count_of_the_rest(db):
    for i in range(8):
        make_ticket(title=f"ticket {i}", priority=i)

    _, body = discord_bot.render_backlog(CFG, limit=3)
    assert "+5 more" in body


def test_a_ticket_view_carries_its_criteria_and_history(db):
    ticket_id = make_ticket(title="add pagination", criteria="- 20 per page")
    tickets.move(ticket_id, tickets.READY, "tester")

    title, body = discord_bot.render_ticket(ticket_id)
    assert f"#{ticket_id}" in title and "add pagination" in title
    assert "20 per page" in body
    assert "ready" in body
    assert "backlog → ready" in body and "tester" in body


def test_a_ticket_without_criteria_says_it_cannot_be_ready(db):
    ticket_id = make_ticket(title="vague idea")

    _, body = discord_bot.render_ticket(ticket_id)
    assert "No acceptance criteria" in body


def test_an_unknown_ticket_renders_nothing_rather_than_raising(db):
    assert discord_bot.render_ticket(9999) is None


def test_rendering_a_view_never_changes_a_ticket(db):
    ticket_id = make_ticket(title="add pagination", criteria="- 20 per page")
    before = tracker.get_backlog_item(ticket_id)
    history = tickets.events(ticket_id)

    discord_bot.render_backlog(CFG)
    discord_bot.render_ticket(ticket_id)

    assert tracker.get_backlog_item(ticket_id) == before
    assert tickets.events(ticket_id) == history
