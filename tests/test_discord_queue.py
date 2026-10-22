"""Discord can read a project's ticket queue and build a ticket from it.

Planning a project produces tickets, not a build. Until these existed a phone
with only /task on it could file *new* work but never touch the queue planning
had produced — the tickets from last night's plan were unreachable from the one
surface the user had. /queue shows that queue and /build pulls from it, under
the same lifecycle, WIP limit and budget as the dashboard and the CLI.

Nothing here talks to Discord: the commands are thin wrappers over the two
functions tested below.
"""
from __future__ import annotations

import pytest

from eng_crew import budget, dispatch, tickets, tracker
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult

discord_bot = pytest.importorskip(
    "eng_crew.discord_bot", reason="discord.py not installed (optional [discord] extra)"
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "queue.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def cfg(tmp_path):
    return Settings(data_dir=tmp_path, wip_limit=3, ticket_budget_usd=15.0)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "planned-last-night"
    root.mkdir()
    return str(root)


@pytest.fixture
def launched(monkeypatch):
    """Capture the build command instead of running it."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dispatch.subprocess, "Popen",
        lambda cmd, **kw: calls.append(list(cmd)) or object(),
    )
    return calls


def _ticket(project: str, title="Web sign-up and auth", criteria="- a user can sign up",
            priority=50, status=tickets.BACKLOG) -> int:
    ticket_id = tracker.add_backlog_item(
        title=title, description=title, project_path=project,
        claude_md_path="", priority=priority, acceptance_criteria=criteria, created_by="planner",
    )
    if status != tickets.BACKLOG:
        tracker.set_ticket_status(ticket_id, tickets.BACKLOG, status, "test", "setup")
    return ticket_id


# ── /queue ──────────────────────────────────────────────────────────────────────

def test_queue_shows_wip_and_every_ticket(db, cfg, project):
    first = _ticket(project, "Next.js web scaffold", priority=10)
    second = _ticket(project, "Web sign-up and auth", priority=20)

    lines = discord_bot.queue_lines({"name": "x4barter", "path": project}, cfg)
    body = "\n".join(lines)

    assert "x4barter" in lines[0] and "0/3 in flight" in lines[0]
    assert f"#{first}" in body and f"#{second}" in body
    # Priority, status, title, criteria and spend against budget all show.
    assert "p10" in body and "backlog" in body
    assert "Next.js web scaffold" in body
    assert "AC ✅" in body
    assert "$0.00/$15.00" in body


def test_queue_flags_a_ticket_with_no_acceptance_criteria(db, cfg, project):
    _ticket(project, "Vague idea", criteria="")
    body = "\n".join(discord_bot.queue_lines({"name": "x4barter", "path": project}, cfg))
    assert "AC ❌" in body


def test_queue_counts_in_flight_work(db, cfg, project):
    _ticket(project, "Being built", status=tickets.IN_PROGRESS)
    _ticket(project, "Waiting", status=tickets.READY)
    lines = discord_bot.queue_lines({"name": "x4barter", "path": project}, cfg)
    assert "1/3 in flight" in lines[0]
    assert "2 slot(s) free" in lines[0]


def test_queue_says_so_when_a_project_has_no_tickets(db, cfg, project):
    lines = discord_bot.queue_lines({"name": "x4barter", "path": project}, cfg)
    assert any("No tickets" in line for line in lines)


def test_an_unknown_project_gets_the_available_aliases():
    reply = discord_bot._unknown_project("x4bartr", {"x4barter": {"path": "/p"}})
    assert "Unknown project `x4bartr`" in reply
    assert "`x4barter`" in reply


# ── /build ──────────────────────────────────────────────────────────────────────

def test_build_approves_a_backlog_ticket_and_starts_it(db, cfg, project, launched):
    ticket_id = _ticket(project)

    reply = discord_bot.build_reply(ticket_id, "alway", cfg)

    assert reply.startswith("🚀")
    assert f"#{ticket_id}" in reply
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_PROGRESS
    assert launched, "no build was launched"
    assert launched[-1][-4:-1] == ["ticket", str(ticket_id), "--no-approval"] or \
        str(ticket_id) in launched[-1]
    # Approving it to ready is the human's move, credited to the Discord user.
    readied = [e for e in tickets.events(ticket_id) if e["to_status"] == tickets.READY]
    assert readied and readied[-1]["actor"] == "alway"


def test_build_starts_a_ticket_that_is_already_ready(db, cfg, project, launched):
    ticket_id = _ticket(project, status=tickets.READY)
    assert discord_bot.build_reply(ticket_id, "alway", cfg).startswith("🚀")
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_PROGRESS


def test_build_never_files_a_new_ticket(db, cfg, project, launched):
    ticket_id = _ticket(project)
    discord_bot.build_reply(ticket_id, "alway", cfg)
    assert [t["id"] for t in tracker.list_project_tickets(project)] == [ticket_id]


def test_build_refuses_a_ticket_with_no_acceptance_criteria(db, cfg, project, launched):
    ticket_id = _ticket(project, criteria="")
    reply = discord_bot.build_reply(ticket_id, "alway", cfg)
    assert reply.startswith("❌") and "acceptance criteria" in reply
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.BACKLOG
    assert not launched


def test_build_refuses_an_unknown_ticket(db, cfg, project, launched):
    reply = discord_bot.build_reply(999999, "alway", cfg)
    assert reply.startswith("❌") and "999999" in reply
    assert not launched


def test_build_refuses_a_ticket_that_has_spent_its_budget(db, cfg, project, launched):
    ticket_id = _ticket(project, status=tickets.READY)
    budget.set_budget(ticket_id, 1.0, "user", settings=cfg)
    run_id = tracker.create_run("build it", project, ticket_id=ticket_id)
    tracker.log_event(run_id, 0, "single_agent", LLMResult(text="", cost_usd=2.5))
    tracker.finish_run(run_id, status="completed")

    reply = discord_bot.build_reply(ticket_id, "alway", cfg)

    assert reply.startswith("⏸️") and "budget" in reply
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY
    assert not launched


def test_build_queues_a_ticket_when_the_project_is_at_its_wip_limit(db, project, launched):
    cfg = Settings(wip_limit=1)
    _ticket(project, "Being built", status=tickets.IN_PROGRESS)
    ticket_id = _ticket(project, "Next in line")

    reply = discord_bot.build_reply(ticket_id, "alway", cfg)

    assert reply.startswith("⏸️")
    assert "WIP limit" in reply and "waiting in the queue" in reply
    # It stays ready — approved, in the queue, nothing started.
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.READY
    assert not launched


def test_build_refuses_a_ticket_that_is_already_building(db, cfg, project, launched):
    ticket_id = _ticket(project, status=tickets.IN_PROGRESS)
    reply = discord_bot.build_reply(ticket_id, "alway", cfg)
    assert reply.startswith("❌") and "in_progress" in reply
    assert not launched


# ── Run it (free-text task from a message) ──────────────────────────────────────
#
# The confirm card used to call the pipeline directly, so a Discord task built
# with no ticket: no criteria, no review, no budget. It now files one.

@pytest.fixture
def drafted(monkeypatch):
    """The Manager's draft, without a model call."""
    from eng_crew import manager

    monkeypatch.setattr(manager, "draft_ticket", lambda task, path, orientation="": {
        "title": "Add a logout button",
        "description": task,
        "acceptance_criteria": "- a signed-in user can log out",
    })


def test_run_it_files_a_ticket_credited_to_the_clicker_and_builds_it(
    db, cfg, project, launched, drafted
):
    reply = discord_bot.task_reply({"name": "demo", "path": project},
                                   "add a logout button", "alway", cfg)

    rows = tracker.list_project_tickets(project)
    assert len(rows) == 1, "Run it files exactly one ticket"
    ticket = rows[0]
    assert (ticket["acceptance_criteria"] or "").strip()
    assert f"#{ticket['id']}" in reply and reply.startswith("🚀")
    assert "Manager drafted" in reply
    ready = [e for e in tickets.events(ticket["id"]) if e["to_status"] == tickets.READY]
    assert ready and ready[0]["actor"] == "alway"
    assert launched, "the ticket's build was started"


def test_run_it_with_no_definition_of_done_files_nothing(db, cfg, project, launched, monkeypatch):
    from eng_crew import manager

    def refuse(*a, **kw):
        raise manager.DraftFailed("the manager could not draft criteria")

    monkeypatch.setattr(manager, "draft_ticket", refuse)
    reply = discord_bot.task_reply({"name": "demo", "path": project}, "do stuff", "alway", cfg)

    assert reply.startswith("❌")
    assert tracker.list_project_tickets(project) == []
    assert launched == []


def test_run_it_at_the_wip_limit_queues_the_ticket_without_building(
    db, project, launched, drafted
):
    cfg = Settings(wip_limit=1, ticket_budget_usd=15.0)
    _ticket(project, "already building", status=tickets.IN_PROGRESS)

    reply = discord_bot.task_reply({"name": "demo", "path": project},
                                   "add a logout button", "alway", cfg)

    assert reply.startswith("⏸️") and "WIP limit" in reply
    assert launched == []
