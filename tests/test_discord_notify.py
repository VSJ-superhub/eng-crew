"""Discord notifications — the ticket reaching out when it needs a person.

A ticket that finishes, gets changes requested, cannot be reviewed, fails its
run, is rejected or runs out of budget is waiting on a human, and until now it
waited silently: whoever dispatched it had to keep the dashboard open to find
out. These tests pin down the one property that makes the feature worth having
— the push comes from the *lifecycle*, so it does not matter which surface
started the build and the Discord bot does not have to be running — and the
properties that keep it from being a liability: it is best-effort, it never
pings anyone it was not told to, and it never leaks the webhook URL.
"""
from __future__ import annotations

import logging

import pytest

from eng_crew import budget, config, notify, tickets, tracker
from eng_crew import run as run_mod
from eng_crew.providers.base import LLMResult

WEBHOOK = "https://discord.com/api/webhooks/123456789/s3cret-t0ken-nobody-may-log"
USER_ID = "987654321"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "notify.db")
    tracker.close_connection()
    tracker._init_db()
    notify.forget_posted()
    yield tmp_path
    tracker.close_connection()
    notify.forget_posted()


@pytest.fixture
def posts(monkeypatch):
    """Every webhook POST this test made: url, payload and timeout."""
    sent: list[dict] = []

    def fake_post(url, *, json=None, timeout=None, **kw):
        sent.append({"url": url, "payload": json, "timeout": timeout})

        class Reply:
            status_code = 204
            text = ""
        return Reply()

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    return sent


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setattr(config.settings, "discord_webhook_url", WEBHOOK)
    monkeypatch.setattr(config.settings, "discord_notify_user_id", "")
    return config.settings


@pytest.fixture
def no_webhook(monkeypatch):
    monkeypatch.setattr(config.settings, "discord_webhook_url", "")
    monkeypatch.setattr(config.settings, "discord_notify_user_id", "")
    return config.settings


# -- helpers ------------------------------------------------------------------

def make_ticket(title="Add pagination to the runs table", project="/proj",
                project_id=7, **kw) -> int:
    ticket_id = tracker.add_backlog_item(
        title, "the details", project, "", project_id=project_id,
        acceptance_criteria="- it works", **kw,
    )
    tickets.move(ticket_id, tickets.READY, "tester")
    return ticket_id


def building(**kw) -> int:
    ticket_id = make_ticket(**kw)
    tickets.move(ticket_id, tickets.IN_PROGRESS, "tester", "build started")
    return ticket_id


def charge(ticket_id: int, amount: float, *, project="/proj",
           status="completed", final_summary="") -> int:
    run_id = tracker.create_run("build it", project, ticket_id=ticket_id)
    tracker.log_event(run_id, 0, "single_agent", LLMResult(text="", cost_usd=amount))
    tracker.finish_run(run_id, status=status, final_summary=final_summary)
    return run_id


def contents(sent: list[dict]) -> list[str]:
    return [p["payload"]["content"] for p in sent]


def history(ticket_id: int) -> str:
    return "\n".join(e["note"] or "" for e in tickets.events(ticket_id))


# -- criterion 1: exactly these transitions post -------------------------------

def test_a_ticket_reaching_done_posts(db, webhook, posts):
    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved — all good")
    posts.clear()
    tickets.move(ticket_id, tickets.DONE, "qa", "verification gate passed")

    assert len(posts) == 1
    assert "done" in contents(posts)[0]


def test_changes_requested_posts(db, webhook, posts):
    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    posts.clear()
    tickets.note(ticket_id, "reviewer",
                 "review: changes_requested — criterion 2 has no test")

    assert len(posts) == 1
    assert "changes requested" in contents(posts)[0]
    assert "criterion 2 has no test" in contents(posts)[0]


def test_review_unavailable_posts(db, webhook, posts):
    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    posts.clear()
    tickets.note(ticket_id, "reviewer",
                 "review unavailable: no API key for the ticket reviewer")

    assert len(posts) == 1
    assert "review unavailable" in contents(posts)[0]
    assert "no API key" in contents(posts)[0]


def test_a_failed_run_posts_with_the_gate_failure_line(db, webhook, posts):
    ticket_id = building()
    run_id = charge(ticket_id, 1.0, status="failed",
                    final_summary="did the work\n\n[VERIFICATION FAILED] FAILED (pytest)")
    posts.clear()
    run_mod._settle_ticket(ticket_id, tickets.REJECTED, "queue",
                           f"run {run_id} finished as failed")

    assert len(posts) == 1
    assert "FAILED (pytest)" in contents(posts)[0]


def test_a_rejected_ticket_posts(db, webhook, posts):
    ticket_id = building()
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "user", "not what we wanted")

    assert len(posts) == 1
    assert "not what we wanted" in contents(posts)[0]


def test_a_budget_stop_posts(db, webhook, posts):
    ticket_id = make_ticket()
    charge(ticket_id, 20.0)
    posts.clear()
    with pytest.raises(budget.BudgetExhausted):
        tickets.move(ticket_id, tickets.IN_PROGRESS, "queue", "build started")

    assert len(posts) == 1
    assert "budget" in contents(posts)[0].lower()
    assert "$20.00" in contents(posts)[0]


def test_no_other_transition_posts(db, webhook, posts):
    """backlog -> ready -> in_progress -> in_review -> in_qa, an area change, a
    budget raise and a ship: every one of them leaves the phone alone."""
    ticket_id = make_ticket()                     # backlog -> ready
    tickets.move(ticket_id, tickets.IN_PROGRESS, "queue", "build started")
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    tickets.note(ticket_id, "reviewer", "review: approve — every criterion met")
    tickets.move(ticket_id, tickets.IN_QA, "reviewer", "review: approved — fine")
    tickets.set_area(ticket_id, "Engine", "user")
    budget.set_budget(ticket_id, 40.0, "user")

    assert posts == []

    tickets.move(ticket_id, tickets.DONE, "qa", "gate passed")
    assert len(posts) == 1                        # done, and only done
    posts.clear()

    tickets.move(ticket_id, tickets.SHIPPED, "user", "live")
    assert posts == []


# -- criterion 2: it fires from the lifecycle, for every surface ---------------

def test_the_push_comes_from_the_lifecycle_not_from_a_surface(db, webhook, posts,
                                                              monkeypatch):
    """No surface and no bot in sight: tickets.move alone notifies.

    Every surface that builds a ticket — the CLI, the dashboard, MCP
    `run_task`, Discord, a queue pull — reaches the same `tickets.move`, so
    this is what makes all of them notify identically. The Discord bot module
    is sabotaged here to prove the notification does not go through it.
    """
    import sys
    monkeypatch.setitem(sys.modules, "eng_crew.discord_bot", None)

    ticket_id = building()
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "queue", "run 9 finished as failed")

    assert len(posts) == 1
    assert posts[0]["url"] == WEBHOOK


# -- criterion 3: what the message says ---------------------------------------

def test_the_message_names_the_ticket_project_spend_reason_and_link(db, webhook, posts):
    ticket_id = building(title="Add pagination to the runs table")
    charge(ticket_id, 3.25)
    posts.clear()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    tickets.note(ticket_id, "reviewer",
                 "review: changes_requested — criterion 3 is untested")

    content = contents(posts)[0]
    assert f"#{ticket_id}" in content
    assert "Add pagination to the runs table" in content
    assert "/proj" in content
    assert "changes requested" in content
    assert "$3.25" in content and "$15.00" in content
    assert "criterion 3 is untested" in content
    assert f"http://127.0.0.1:9000/projects/7/tasks?ticket={ticket_id}" in content


# -- criterion 4: mentions ----------------------------------------------------

def test_a_configured_user_is_mentioned_and_is_the_only_allowed_mention(
        db, webhook, posts, monkeypatch):
    monkeypatch.setattr(config.settings, "discord_notify_user_id", USER_ID)
    ticket_id = building()
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "queue", "run 1 finished as failed")

    payload = posts[0]["payload"]
    assert f"<@{USER_ID}>" in payload["content"]
    assert payload["allowed_mentions"] == {"parse": [], "users": [USER_ID]}


def test_with_no_user_configured_nobody_is_mentioned(db, webhook, posts):
    ticket_id = building()
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "queue", "run 1 finished as failed")

    payload = posts[0]["payload"]
    assert "<@" not in payload["content"]
    assert payload["allowed_mentions"] == {"parse": [], "users": []}


def test_mentions_in_ticket_text_never_ping_anyone(db, webhook, posts, monkeypatch):
    monkeypatch.setattr(config.settings, "discord_notify_user_id", USER_ID)
    ticket_id = building(title="@everyone ping @here and <@111222333> too")
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "queue",
                 "rejected by @everyone and <@&444555>")

    payload = posts[0]["payload"]
    # The API side: only the configured user may be mentioned at all.
    assert payload["allowed_mentions"] == {"parse": [], "users": [USER_ID]}
    # And the text side, so a client that ignores allowed_mentions is harmless.
    for ping in ("@everyone", "@here", "<@111222333>", "<@&444555>"):
        assert ping not in payload["content"]
    assert f"<@{USER_ID}>" in payload["content"]


# -- criterion 5: no webhook, no change in behaviour --------------------------

def test_with_no_webhook_configured_nothing_is_posted(db, no_webhook, posts):
    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    tickets.note(ticket_id, "reviewer", "review: changes_requested — nope")
    tickets.move(ticket_id, tickets.REJECTED, "user", "dropped")

    assert posts == []
    assert tickets.events(ticket_id)          # the lifecycle ran exactly as before


# -- criterion 6: best effort -------------------------------------------------

@pytest.mark.parametrize("failure", ["raise", "non-2xx"])
def test_a_broken_webhook_never_breaks_the_transition(db, webhook, monkeypatch, failure):
    import httpx

    def fake_post(url, *, json=None, timeout=None, **kw):
        if failure == "raise":
            raise httpx.ConnectTimeout("timed out")

        class Reply:
            status_code = 500
            text = "internal error"
        return Reply()

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    ticket_id = building()
    ticket = tickets.move(ticket_id, tickets.REJECTED, "queue", "run 1 finished as failed")
    assert ticket["status"] == tickets.REJECTED


def test_the_post_is_bounded_by_a_short_timeout(db, webhook, posts):
    ticket_id = building()
    posts.clear()
    tickets.move(ticket_id, tickets.REJECTED, "queue", "run 1 finished as failed")

    assert 0 < posts[0]["timeout"] <= 5


# -- criterion 7: the URL is a secret -----------------------------------------

def test_the_webhook_url_is_never_logged_or_recorded(db, webhook, monkeypatch, caplog):
    import httpx

    def fake_post(url, *, json=None, timeout=None, **kw):
        raise httpx.ConnectError(f"could not reach {url}")

    monkeypatch.setattr(notify.httpx, "post", fake_post)

    with caplog.at_level(logging.DEBUG):
        ticket_id = building()
        tickets.move(ticket_id, tickets.REJECTED, "queue", "run 1 finished as failed")

    secret = WEBHOOK.rsplit("/", 1)[-1]
    assert secret not in caplog.text
    assert WEBHOOK not in caplog.text
    assert secret not in history(ticket_id)
    assert WEBHOOK not in history(ticket_id)


# -- criterion 8: once per transition -----------------------------------------

def test_the_same_event_is_not_posted_twice(db, webhook, posts):
    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    posts.clear()

    note = "review: changes_requested — criterion 2 has no test"
    tickets.note(ticket_id, "reviewer", note)
    tickets.note(ticket_id, "reviewer", note)

    assert len(posts) == 1


# -- criterion 9: it is documented --------------------------------------------

@pytest.mark.parametrize("doc", [".env.example", "CLAUDE.md", "README.md"])
def test_the_settings_are_documented(doc):
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / doc).read_text(encoding="utf-8")
    assert "ENG_CREW_DISCORD_WEBHOOK_URL" in text
    assert "ENG_CREW_DISCORD_NOTIFY_USER_ID" in text


# -- the notes are recognised as their writers write them ----------------------
#
# The notifier recognises these events by how the reviewer and the budget word
# their notes. Producing each note through the code that really writes it, not
# a copy of the wording, means rewording one fails here instead of silently
# ending the notification.

def test_the_reviewers_own_changes_requested_note_posts(db, webhook, posts):
    from eng_crew import review

    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    posts.clear()
    tickets.note(ticket_id, review.REVIEWER_ACTOR,
                 review._note(review.CHANGES_REQUESTED, "criterion 2 has no test"))

    assert len(posts) == 1
    assert "changes requested" in contents(posts)[0]
    assert "criterion 2 has no test" in contents(posts)[0]


def test_the_reviewers_own_unavailable_note_posts(db, webhook, posts):
    from eng_crew import review

    ticket_id = building()
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue", "run 1 finished as completed")
    posts.clear()
    review._unavailable(ticket_id, "no API key for the ticket reviewer")

    assert len(posts) == 1
    assert "review unavailable" in contents(posts)[0]
    assert "no API key" in contents(posts)[0]


def test_the_budgets_own_stop_note_posts(db, webhook, posts):
    ticket_id = building()
    posts.clear()
    budget._record_stop(ticket_id, "a review", {"spent_usd": 15.94, "budget_usd": 15.0})

    assert len(posts) == 1
    assert "15.94" in contents(posts)[0]


def test_a_push_trusts_the_operating_systems_certificates_too(webhook, monkeypatch):
    # An antivirus web shield or a corporate proxy re-signs HTTPS with a root
    # only the OS store holds; httpx's certifi-only default rejects every push.
    seen: dict = {}

    def fake_post(url, *, json=None, timeout=None, verify=True, **kw):
        seen["verify"] = verify

        class Reply:
            status_code = 204
            text = ""
        return Reply()

    loaded: list[bool] = []
    monkeypatch.setattr(notify.ssl.SSLContext, "load_default_certs",
                        lambda self, *a, **kw: loaded.append(True))
    notify.tls_context.cache_clear()
    monkeypatch.setattr(notify.httpx, "post", fake_post)
    try:
        assert notify.post({"content": "hi"}, webhook) is True
        assert isinstance(seen["verify"], notify.ssl.SSLContext)
        assert loaded == [True]
    finally:
        notify.tls_context.cache_clear()
