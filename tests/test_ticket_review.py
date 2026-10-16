"""The independent review step between a landed run and a done ticket.

A ticket used to stop at `in_review` forever: the run that built it was also the
last thing to look at it, and nothing ever said whether the acceptance criteria
were met. These tests pin the reviewer down as *independent* — a different role,
a cheap model with no file tools, credited as itself in the ticket's history —
and pin the two things it may and may not do: walk an approved, verified ticket
through QA to done, and never ship one.

Every test stubs the provider seam (`agents.base.call_llm`), so the reviewer's
own bookkeeping — the cost event, the run total — stays real and asserted.
"""
from __future__ import annotations

import pytest

from eng_crew import review, tickets, tracker
from eng_crew.agents import base as agent_base
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "review.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def reviewer(monkeypatch):
    """Stub the one LLM call. Records prompts; the verdict is settable."""
    calls: list[dict] = []
    box = {"text": "APPROVE\nEvery criterion is covered."}

    def fake_call_llm(provider, model, prompt, **kwargs):
        calls.append({"provider": provider, "model": model, "prompt": prompt,
                      "kwargs": kwargs})
        return LLMResult(
            text=box["text"], input_tokens=900, output_tokens=60,
            cost_usd=0.0013, provider=provider, model=model,
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Settings() reads the developer's real .env; pin the reviewer to the
    # default API provider so a local ENG_CREW_TICKET_REVIEWER_PROVIDER does not
    # leak in. Tests that want the CLI path pass it to Settings() explicitly.
    monkeypatch.setenv("ENG_CREW_TICKET_REVIEWER_PROVIDER", "anthropic")
    monkeypatch.setattr(agent_base, "call_llm", fake_call_llm)
    return {"calls": calls, "box": box}


DIFF = "--- a/app.py\n+++ b/app.py\n+button.color = BLUE\n"


def landed_ticket(*, run_status="completed", criteria="- the button is blue",
                  diff=DIFF):
    """A ticket whose run is over and which has been handed to review."""
    from eng_crew import run as run_mod

    ticket_id = tracker.add_backlog_item(
        "Make the button blue", "the details", "/proj", "",
        acceptance_criteria=criteria,
    )
    tickets.move(ticket_id, tickets.READY, "tester")

    launches: list[dict] = []

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        launches.append({"task": task, "run_id": run_id})
        tracker.finish_run(run_id, status=run_status)
        return {"git_branch": "ai-team/x", "main_project_path": project_path}

    patch = pytest.MonkeyPatch()
    patch.setattr(run_mod, "run_pipeline", fake_pipeline)
    patch.setattr(review, "run_diff", lambda state, **kw: diff)
    try:
        run_mod.run_ticket(ticket_id, actor="queue", settings=Settings())
    finally:
        patch.undo()
    return ticket_id, launches


def in_review_ticket(criteria="- it works") -> int:
    """A ticket parked at in_review, with a run, and nothing reviewed yet."""
    ticket_id = tracker.add_backlog_item("t", "d", "/proj", "",
                                         acceptance_criteria=criteria)
    tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tickets.move(ticket_id, tickets.READY, "tester")
    tickets.move(ticket_id, tickets.IN_PROGRESS, "queue")
    tickets.move(ticket_id, tickets.IN_REVIEW, "queue")
    return ticket_id


def notes_of(ticket_id: int) -> str:
    return "\n".join(e["note"] for e in tickets.events(ticket_id))


# --- criterion 1: a verdict, with reasons, in the ticket's history -------


def test_a_landed_run_is_reviewed_against_the_criteria_and_the_verdict_recorded(db, reviewer):
    reviewer["box"]["text"] = "CHANGES_REQUESTED\nThe button is still green."
    ticket_id, _ = landed_ticket()

    prompt = reviewer["calls"][0]["prompt"]
    assert "the button is blue" in prompt, "the reviewer never saw the acceptance criteria"
    assert "button.color" in prompt, "the reviewer never saw the run's diff"

    verdicts = [e for e in tickets.events(ticket_id) if e["actor"] == review.REVIEWER_ACTOR]
    assert verdicts, "the review left no trace in the ticket's history"
    assert "changes_requested" in notes_of(ticket_id)
    assert "still green" in notes_of(ticket_id), "the reviewer's reasons were not recorded"


def test_an_approval_is_recorded_with_its_reasons_too(db, reviewer):
    reviewer["box"]["text"] = "APPROVE\nAll three criteria are covered by tests."
    ticket_id, _ = landed_ticket()

    assert "approve" in notes_of(ticket_id)
    assert "three criteria" in notes_of(ticket_id)


# --- criterion 2: approved + verified walks through QA to done ------------


def test_an_approved_verified_ticket_moves_through_qa_to_done(db, reviewer):
    reviewer["box"]["text"] = "APPROVE\nCriteria met."
    ticket_id, _ = landed_ticket(run_status="completed")

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE

    moves = {(e["from_status"], e["to_status"]): e["actor"]
             for e in tickets.events(ticket_id)}
    assert moves[(tickets.IN_REVIEW, tickets.IN_QA)] == review.REVIEWER_ACTOR
    assert moves[(tickets.IN_QA, tickets.DONE)] == review.QA_ACTOR


def test_an_approved_ticket_whose_run_missed_the_gate_does_not_reach_done(db, reviewer):
    """"unverified" means the work landed but nothing vouched for it — an
    approving reviewer is not a substitute for the gate."""
    reviewer["box"]["text"] = "APPROVE\nLooks right to me."
    ticket_id, _ = landed_ticket(run_status="unverified")

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    assert "verification" in notes_of(ticket_id).lower()


# --- criterion 3: changes requested stops there, and starts nothing ------


def test_changes_requested_keeps_the_ticket_out_of_done_and_starts_no_build(db, reviewer):
    reviewer["box"]["text"] = "CHANGES_REQUESTED\nNo test covers the second criterion."
    ticket_id, launches = landed_ticket()

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    assert "second criterion" in notes_of(ticket_id)
    assert len(launches) == 1, "the changes-requested verdict started another build"


def test_the_ticket_endpoint_shows_the_reviewers_reasons(db, reviewer):
    """A ticket held back at in_review has to say why, where someone will see it."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    reviewer["box"]["text"] = "CHANGES_REQUESTED\nNothing implements the blue button."
    ticket_id, _ = landed_ticket()

    with TestClient(app) as client:
        body = client.get(f"/api/tickets/{ticket_id}").json()

    assert body["review"]["verdict"] == review.CHANGES_REQUESTED
    assert "blue button" in body["review"]["reasons"]


# --- criterion 1: a review that could not run is not a rejection ---------


def test_a_reviewer_that_cannot_be_reached_is_not_recorded_as_a_rejection(
    db, reviewer, monkeypatch
):
    """A provider that is down says nothing about the work. Recording that as
    changes_requested blames the build for the reviewer's outage."""
    def boom(*a, **kw):
        raise RuntimeError("the provider died")

    monkeypatch.setattr(agent_base, "call_llm", boom)
    ticket_id, _ = landed_ticket()

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    notes = notes_of(ticket_id)
    assert review.CHANGES_REQUESTED not in notes, \
        "an unreachable reviewer was recorded as a rejection"
    assert "review unavailable" in notes.lower(), \
        "the history does not say the review was unavailable"
    assert "the provider died" in notes, "the history does not say why"

    verdict = review.latest_verdict(ticket_id)
    assert verdict["verdict"] == review.UNAVAILABLE


def test_a_reviewer_with_no_api_key_is_not_recorded_as_a_rejection(db, reviewer, monkeypatch):
    """The out-of-the-box setup for a new user: a provider and no key for it."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(ticket_reviewer_provider="anthropic",
                        ticket_reviewer_model="claude-haiku-4-5-20251001",
                        anthropic_api_key="")
    ticket_id = in_review_ticket()

    with pytest.raises(review.ReviewUnavailable, match="ANTHROPIC_API_KEY"):
        review.review_ticket(ticket_id, diff="x", settings=settings)

    assert reviewer["calls"] == [], "a reviewer with no credentials was called anyway"
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    notes = notes_of(ticket_id)
    assert review.CHANGES_REQUESTED not in notes
    assert "review unavailable" in notes.lower()
    assert "ANTHROPIC_API_KEY" in notes, "the history does not name what is missing"


# --- criterion 4: one cheap call, no file tools, cost on the run ---------


def test_the_review_is_one_call_on_a_model_that_cannot_edit_files(db, reviewer):
    landed_ticket()

    assert len(reviewer["calls"]) == 1, "the reviewer is not one single call"
    call = reviewer["calls"][0]
    # A CLI provider runs an agentic session that can edit files; a plain API
    # call returns text and nothing else.
    assert call["provider"] not in ("claude_cli", "gemini_cli")
    assert call["model"] != Settings().get_agent_config("single_agent")["model"], \
        "the reviewer is using the builder's model"


def test_a_cli_only_setup_reviews_through_the_claude_cli_with_no_tools(db, reviewer):
    """A CLI provider is an agentic session, so it could edit what it judges —
    but the Claude CLI can be run with every file and shell tool disabled, and a
    user whose only provider is the CLI should still get a review."""
    settings = Settings(ticket_reviewer_provider="claude_cli",
                        ticket_reviewer_model="claude-haiku-4-5-20251001")
    reviewer["box"]["text"] = "APPROVE" + chr(10) + "The criteria are met."
    ticket_id = in_review_ticket()

    verdict = review.review_ticket(ticket_id, diff=DIFF, settings=settings)

    assert verdict["verdict"] == review.APPROVE
    assert len(reviewer["calls"]) == 1, "the CLI-only reviewer never ran"
    call = reviewer["calls"][0]
    assert call["provider"] == "claude_cli"

    disallowed = call["kwargs"].get("disallowed_tools") or ""
    for tool in ("Write", "Edit", "Bash", "Read"):
        assert tool in disallowed, f"the reviewing CLI could still use {tool}"
    assert not call["kwargs"].get("allowed_tools"), \
        "the reviewing CLI was handed an allow-list of tools"
    assert call["kwargs"].get("no_mcp"), "the reviewing CLI could reach MCP servers"


def test_the_reviews_cost_is_recorded_against_the_run(db, reviewer):
    ticket_id, _ = landed_ticket()

    run = tracker.get_ticket_run(ticket_id)
    events = [e for e in tracker.get_run_events(run["id"])
              if e["agent_name"] == review.REVIEWER_ROLE]
    assert len(events) == 1
    assert events[0]["cost_usd"] == pytest.approx(0.0013)
    assert tracker.get_run_detail(run["id"])["total_cost_usd"] == pytest.approx(0.0013)


# --- criterion 5: shipping stays human ----------------------------------


def test_shipping_a_done_ticket_stays_a_human_action(db, reviewer):
    reviewer["box"]["text"] = "APPROVE\nCriteria met."
    ticket_id, _ = landed_ticket()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE

    for robot in (review.REVIEWER_ACTOR, review.QA_ACTOR, "queue"):
        with pytest.raises(tickets.TransitionRefused, match="person"):
            tickets.move(ticket_id, tickets.SHIPPED, robot)
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE

    assert tickets.move(ticket_id, tickets.SHIPPED, "alway")["status"] == tickets.SHIPPED


# --- criterion 6: a truncated diff says what it left out -----------------
#
# Ticket #100646: a complete change was reviewed through a diff cut at
# MAX_DIFF_CHARS, and the reviewer requested changes because the code for a
# file past the cut was "missing". It was not missing; it was never shown.


def _diff_section(path: str, lines: int) -> str:
    """One file's worth of unified diff, long enough to push past the cut."""
    body = "".join(f"+line {i} of {path}\n" for i in range(lines))
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{lines} @@\n{body}")


def truncated_diff(paths: list[str]) -> str:
    """A diff over several files, cut the way git_skill.branch_diff cuts it."""
    from eng_crew import git_skill

    # The first file's changes fit inside the cut; the rest fall past it.
    text = "".join(_diff_section(p, 1000 if i == 0 else 200)
                   for i, p in enumerate(paths))
    assert len(text) > review.MAX_DIFF_CHARS, "this diff is not big enough to truncate"
    return text[:review.MAX_DIFF_CHARS] + git_skill.TRUNCATION_NOTE


def test_a_truncated_diff_still_tells_the_reviewer_every_file_the_branch_changed(
    db, reviewer
):
    files = ["eng_crew/app.py", "eng_crew/tracker.py", "tests/test_app.py"]
    ticket_id = in_review_ticket()

    review.review_ticket(ticket_id, diff=truncated_diff(files), files=files,
                         settings=Settings())

    prompt = reviewer["calls"][0]["prompt"]
    for path in files:
        assert path in prompt, f"the reviewer was never told {path} was changed"


def test_the_prompt_names_the_files_the_truncated_diff_left_out(db, reviewer):
    files = ["eng_crew/app.py", "eng_crew/tracker.py", "tests/test_app.py"]
    ticket_id = in_review_ticket()

    review.review_ticket(ticket_id, diff=truncated_diff(files), files=files,
                         settings=Settings())

    prompt = reviewer["calls"][0]["prompt"]
    _, _, not_shown = prompt.partition(review.NOT_SHOWN_HEADING)
    assert not_shown, "the prompt has no 'files not shown' section"
    assert "eng_crew/tracker.py" in not_shown, "the cut file is not listed as unshown"
    assert "tests/test_app.py" in not_shown
    assert "eng_crew/app.py" not in not_shown, \
        "a file the reviewer was fully shown is listed as not shown"


def test_the_skill_forbids_requesting_changes_over_a_file_it_was_not_shown():
    from eng_crew import prompts

    body = prompts.load("review-ticket", refresh=True).body.lower()
    assert "not shown" in body, "the template never mentions unshown files"
    rule = body.split("not shown", 1)[1]
    assert "changes_requested" in rule, \
        "the template does not forbid CHANGES_REQUESTED over a file it was not shown"


def test_a_file_cut_from_a_real_branch_diff_is_reported_as_unshown(db, reviewer, tmp_path):
    """The #100646 reproduction, on a real repository: the change is complete,
    the diff is not, and the reviewer is told which file it is missing."""
    import subprocess

    from eng_crew import git_skill

    root = tmp_path / "proj"
    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "tracker.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

    subprocess.run(["git", "checkout", "-q", "-b", "ai-team/big"], cwd=root, check=True)
    (root / "app.py").write_text("".join(f"line {i}\n" for i in range(4000)),
                                 encoding="utf-8")
    (root / "tracker.py").write_text("x = 2  # the change the reviewer wants\n",
                                     encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "big change"], cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)

    state = {"git_branch": "ai-team/big", "main_project_path": str(root)}
    diff = review.run_diff(state)
    assert git_skill.TRUNCATION_NOTE in diff, "the diff was not truncated"
    assert "the change the reviewer wants" not in diff, "tracker.py survived the cut"

    ticket_id = in_review_ticket()
    review.review_ticket(ticket_id, diff=diff, files=review.changed_files(state),
                         settings=Settings())

    prompt = reviewer["calls"][0]["prompt"]
    _, _, not_shown = prompt.partition(review.NOT_SHOWN_HEADING)
    assert "tracker.py" in not_shown, \
        "the reviewer could cite tracker.py as missing with nothing to say it was cut"


def test_an_untruncated_diff_adds_no_files_not_shown_notice(db, reviewer):
    ticket_id = in_review_ticket()

    review.review_ticket(ticket_id, diff=DIFF, files=["app.py"], settings=Settings())

    prompt = reviewer["calls"][0]["prompt"]
    assert review.NOT_SHOWN_HEADING not in prompt, \
        "a complete diff was given a 'files not shown' notice anyway"
    assert "button.color" in prompt
