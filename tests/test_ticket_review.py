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

    # The first file's changes fit inside the cut; the rest fall past it. Sized
    # from the limit (~30 chars a line), so a new limit does not break this.
    first, rest = review.MAX_DIFF_CHARS // 40, review.MAX_DIFF_CHARS // 80
    text = "".join(_diff_section(p, first if i == 0 else rest)
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
    (root / "app.py").write_text("".join(f"line {i}\n" for i in range(review.MAX_DIFF_CHARS // 8)),
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


# --- reading the verdict ------------------------------------------------
#
# A reviewer wrote "APPROVE — every acceptance criterion is met by this diff"
# and eng-crew filed it as changes requested, because only a first line that
# was the verdict word alone could be read.


@pytest.mark.parametrize("reply, verdict", [
    ("APPROVE", review.APPROVE),
    ("APPROVE — every acceptance criterion is met by this diff", review.APPROVE),
    ("**Approved**: the criteria are met", review.APPROVE),
    ("CHANGES_REQUESTED\n- the button is red", review.CHANGES_REQUESTED),
    ("Changes requested — the button is red", review.CHANGES_REQUESTED),
])
def test_a_verdict_is_read_from_the_start_of_the_first_line(reply, verdict):
    assert review.parse_verdict(reply)[0] == verdict


def test_reasons_on_the_verdict_line_are_kept():
    _, reasons = review.parse_verdict("APPROVE — every criterion is met")
    assert reasons == "every criterion is met"


def test_a_reply_with_no_verdict_is_still_not_an_approval():
    verdict, reasons = review.parse_verdict("Looks mostly fine to me")
    assert verdict == review.CHANGES_REQUESTED
    assert "unreadable" in reasons


def test_a_usage_limit_reply_is_review_unavailable_not_a_rejection(db, reviewer, monkeypatch):
    """The CLI's error result is raised, so nobody mistakes it for a verdict."""
    from eng_crew.providers.claude_cli import CLIErrorResult

    def limit(*a, **kw):
        raise CLIErrorResult("You've hit your session limit · resets 12:50pm")

    monkeypatch.setattr(agent_base.BaseAgent, "call", limit)
    ticket_id = in_review_ticket()

    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id)
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    assert review.latest_verdict(ticket_id)["verdict"] == review.UNAVAILABLE
    assert "changes_requested" not in notes_of(ticket_id)


# --- what the review is measured against ---------------------------------
#
# Ticket #100765's run was built on #100758's unmerged branch. Diffed against
# HEAD, the reviewer would have judged #100758's sign-up work as #100765's.


def _git_repo(root):
    import subprocess

    root.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "app.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


def _commit_on(root, branch, start, name, text):
    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", branch, start], cwd=root, check=True)
    (root / name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=root, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=root, check=True)


def stacked_run(tmp_path, ticket_id=None):
    """A run whose branch forked from a prerequisite's unmerged branch."""
    root = _git_repo(tmp_path / "proj")
    _commit_on(root, "ai-team/prereq", "main", "signup.py", "PREREQ_WORK = 1\n")
    _commit_on(root, "ai-team/feature", "ai-team/prereq", "listings.py", "FEATURE_WORK = 1\n")
    run_id = tracker.create_run("feature", str(root), ticket_id=ticket_id)
    tracker.update_run_branch(run_id, "ai-team/feature")
    tracker.update_run_base(run_id, "ai-team/prereq", "#1's branch")
    return root, tracker.get_run_detail(run_id)


def test_a_stacked_run_is_reviewed_against_the_branch_it_was_built_on(db, tmp_path):
    _, run = stacked_run(tmp_path)

    diff = review.run_diff(run)

    assert "FEATURE_WORK" in diff
    assert "PREREQ_WORK" not in diff, "the prerequisite's work was judged as this ticket's"
    assert review.changed_files(run) == ["listings.py"]


def test_the_pipelines_state_finds_the_base_through_the_run(db, tmp_path):
    root, run = stacked_run(tmp_path)
    state = {"git_branch": "ai-team/feature", "main_project_path": str(root),
             "run_id": run["id"]}

    assert review.review_base(state) == "ai-team/prereq"
    assert "PREREQ_WORK" not in review.run_diff(state)


def test_a_base_that_no_longer_exists_falls_back_to_head(db, tmp_path):
    import subprocess

    root, run = stacked_run(tmp_path)
    subprocess.run(["git", "merge", "-q", "--no-edit", "ai-team/prereq"], cwd=root, check=True)
    subprocess.run(["git", "branch", "-qD", "ai-team/prereq"], cwd=root, check=True)

    assert review.review_base(run) == "HEAD"
    diff = review.run_diff(run)
    assert "FEATURE_WORK" in diff and "PREREQ_WORK" not in diff


def test_a_run_forked_from_head_is_still_measured_against_head(db, tmp_path):
    root = _git_repo(tmp_path / "proj")
    _commit_on(root, "ai-team/plain", "main", "x.py", "X = 1\n")
    run_id = tracker.create_run("plain", str(root))
    tracker.update_run_branch(run_id, "ai-team/plain")
    tracker.update_run_base(run_id, "HEAD", "the project checkout's HEAD")
    run = tracker.get_run_detail(run_id)

    assert review.review_base(run) == "HEAD"
    assert "X = 1" in review.run_diff(run)


def test_a_review_handed_no_diff_reads_it_from_the_run(db, reviewer, tmp_path):
    """Called the way a resumed review calls it: ticket and run, nothing else."""
    ticket_id = in_review_ticket()
    _, run = stacked_run(tmp_path, ticket_id=ticket_id)

    review.review_ticket(ticket_id, run["id"], settings=Settings())

    prompt = reviewer["calls"][0]["prompt"]
    assert "FEATURE_WORK" in prompt
    assert "PREREQ_WORK" not in prompt
    assert "(the diff could not be read)" not in prompt


# --- an approval that did not see everything ------------------------------


def test_an_approval_that_did_not_see_every_file_keeps_the_ticket_in_review(db, reviewer):
    files = ["api/listings.py", "apps/web/ListingForm.tsx", "apps/web/ProfileForm.tsx"]
    ticket_id = in_review_ticket()
    tracker.finish_run(tracker.get_ticket_run(ticket_id)["id"], status="completed")

    verdict = review.review_ticket(ticket_id, diff=truncated_diff(files), files=files,
                                   settings=Settings())

    assert verdict["verdict"] == review.APPROVE
    assert verdict["status"] == tickets.IN_REVIEW
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    notes = notes_of(ticket_id)
    assert "1 of 3 changed files" in notes
    assert "apps/web/ProfileForm.tsx" in notes
    assert review.latest_verdict(ticket_id)["partial"] is True
    # A verdict was given; it is a person's to act on, not a reason to ask again.
    assert review.awaiting_review(ticket_id) is None


def test_the_diff_limit_is_configurable():
    assert review.max_diff_chars(Settings(ticket_review_max_diff_chars=5000)) == 5000
    assert review.max_diff_chars(Settings()) == review.MAX_DIFF_CHARS


# --- a review the budget stopped runs once the budget allows it -----------


def over_budget_in_review(budget_usd=15.0, spent=21.35) -> tuple[int, int]:
    """#100765: the build and its repair spent past the budget, so the review
    after them never started."""
    ticket_id = tracker.add_backlog_item("t", "d", "/proj", "",
                                         acceptance_criteria="- it works",
                                         budget_usd=budget_usd)
    tickets.move(ticket_id, tickets.READY, "tester")
    tickets.move(ticket_id, tickets.IN_PROGRESS, "queue")
    run_id = tracker.create_run("t", "/proj", ticket_id=ticket_id)
    tracker.log_event(run_id, 0, "single_agent", LLMResult(text="", cost_usd=spent))
    tracker.finish_run(run_id, status="completed")
    tickets.move(ticket_id, tickets.IN_REVIEW, "cli", f"run {run_id} finished as completed")
    return ticket_id, run_id


def test_a_review_stopped_for_budget_leaves_the_ticket_waiting_on_one(db, reviewer):
    ticket_id, run_id = over_budget_in_review()

    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id, run_id, settings=Settings())

    assert reviewer["calls"] == []
    assert review.awaiting_review(ticket_id) == run_id
    assert review.review_if_waiting(ticket_id, Settings()) is None   # still no money
    assert reviewer["calls"] == []


def test_raising_the_budget_lets_the_waiting_review_run(db, reviewer, monkeypatch):
    from eng_crew import budget

    monkeypatch.setattr(review, "run_diff", lambda state, **kw: DIFF)
    ticket_id, run_id = over_budget_in_review()
    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id, run_id, settings=Settings())

    budget.set_budget(ticket_id, 25.0, actor="alway")
    verdict = review.review_if_waiting(ticket_id, Settings())

    assert verdict["verdict"] == review.APPROVE
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE
    assert review.awaiting_review(ticket_id) is None


def test_a_ticket_that_already_has_a_verdict_is_not_reviewed_again(db, reviewer):
    reviewer["box"]["text"] = "CHANGES_REQUESTED\n- the button is red"
    ticket_id, _ = landed_ticket()
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW

    assert review.awaiting_review(ticket_id) is None
    assert review.review_if_waiting(ticket_id, Settings()) is None
    assert len(reviewer["calls"]) == 1


def _inline_threads(monkeypatch):
    """Run the dashboard's background review inline, so a test can see it."""
    import types

    from eng_crew.dashboard.routers import projects

    class Inline:
        def __init__(self, target, **kw):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(projects, "threading", types.SimpleNamespace(Thread=Inline))


def test_raising_the_budget_on_the_dashboard_starts_the_waiting_review(db, reviewer, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    monkeypatch.setattr(review, "run_diff", lambda state, **kw: DIFF)
    _inline_threads(monkeypatch)
    ticket_id, run_id = over_budget_in_review()
    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id, run_id, settings=Settings())

    with TestClient(app) as client:
        assert client.get(f"/api/tickets/{ticket_id}").json()["awaiting_review"] is True
        body = client.put(f"/api/tickets/{ticket_id}/budget",
                          json={"budget_usd": 25.0, "actor": "alway"}).json()

    assert body["review_started"] is True
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE


def test_the_dashboard_review_button_refuses_a_ticket_with_a_verdict(db, reviewer, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    _inline_threads(monkeypatch)
    reviewer["box"]["text"] = "CHANGES_REQUESTED\n- the button is red"
    ticket_id, _ = landed_ticket()

    with TestClient(app) as client:
        refused = client.post(f"/api/tickets/{ticket_id}/review")

    assert refused.status_code == 409
    assert len(reviewer["calls"]) == 1


def test_the_cli_review_command_runs_a_waiting_review(db, reviewer, monkeypatch):
    from typer.testing import CliRunner

    from eng_crew import cli

    monkeypatch.setattr(review, "run_diff", lambda state, **kw: DIFF)
    ticket_id, _ = over_budget_in_review(budget_usd=50.0)

    out = CliRunner().invoke(cli.app, ["review", str(ticket_id)])

    assert out.exit_code == 0, out.output
    assert "approve" in out.output
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.DONE


# --- one verdict per wait: concurrent requests do not pay twice -----------


def test_a_second_review_of_a_ticket_already_under_review_does_not_run(db, reviewer, monkeypatch):
    """Run review clicked twice: both requests saw the ticket waiting, and a
    second verdict landed nine seconds later on a ticket already done."""
    ticket_id = in_review_ticket()
    monkeypatch.setattr(review, "run_diff", lambda state, **kw: DIFF)
    inner: list = []
    stub = agent_base.call_llm

    def call_while_reviewing(provider, model, prompt, **kwargs):
        # The second request arrives while the first waits on the model.
        assert review.reviewing(ticket_id)
        inner.append(review.review_ticket(ticket_id, settings=Settings()))
        return stub(provider, model, prompt, **kwargs)

    monkeypatch.setattr(agent_base, "call_llm", call_while_reviewing)

    first = review.review_ticket(ticket_id, settings=Settings())

    assert inner == [None]
    assert len(reviewer["calls"]) == 1
    assert first["verdict"] == review.APPROVE
    assert notes_of(ticket_id).count("review: approve") == 1
    assert not review.reviewing(ticket_id)


def test_a_verdict_is_not_recorded_on_a_ticket_that_moved_on_during_the_review(
        db, reviewer, monkeypatch):
    """Another process gave its verdict first; this one's is dropped."""
    ticket_id = in_review_ticket()
    monkeypatch.setattr(review, "run_diff", lambda state, **kw: DIFF)
    stub = agent_base.call_llm

    def ticket_moves_meanwhile(provider, model, prompt, **kwargs):
        tickets.move(ticket_id, tickets.REJECTED, "user")
        return stub(provider, model, prompt, **kwargs)

    monkeypatch.setattr(agent_base, "call_llm", ticket_moves_meanwhile)

    assert review.review_ticket(ticket_id, settings=Settings()) is None
    assert "review: approve" not in notes_of(ticket_id)
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.REJECTED


def test_the_dashboard_refuses_a_review_that_is_already_running(db, monkeypatch):
    from fastapi.testclient import TestClient
    from eng_crew.dashboard.app import app

    ticket_id = in_review_ticket()
    monkeypatch.setattr(review, "reviewing", lambda tid: tid == ticket_id)

    response = TestClient(app).post(f"/api/tickets/{ticket_id}/review")

    assert response.status_code == 409
    assert "already running" in response.json()["error"]


# --- "can't judge, the file was cut" is not a rejection ------------------
#
# The prompt tells the reviewer not to request changes over a file it was never
# shown — but it only ever had two verdicts to answer with, and anything else
# fell through parse_verdict's unrecognised-token default to changes_requested.
# A reviewer saying "I cannot judge criterion 2 without tracker.py" is a review
# that could not run, not a judgement on the build.


def test_the_skill_offers_a_token_for_a_criterion_it_cannot_judge():
    from eng_crew import prompts

    body = prompts.load("review-ticket", refresh=True).body
    assert review.UNABLE_TO_JUDGE_TOKEN in body, \
        "the template gives the reviewer no way to say it cannot judge a criterion"
    rule = body.split(review.UNABLE_TO_JUDGE_TOKEN, 1)[1].lower()
    assert "not shown" in rule, \
        "the template does not tie the token to a file it was not shown"
    # It is the narrow exception, not an escape hatch: the two real verdicts are
    # still what a reviewer that has seen enough must answer with.
    assert "APPROVE" in body and "CHANGES_REQUESTED" in body


def test_an_unable_to_judge_reply_is_recorded_as_a_review_that_could_not_run(
    db, reviewer
):
    reviewer["box"]["text"] = (
        f"{review.UNABLE_TO_JUDGE_TOKEN} — criterion 2 turns entirely on "
        "eng_crew/tracker.py, which was not shown to me."
    )
    ticket_id = in_review_ticket()

    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id, diff=DIFF, settings=Settings())

    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    notes = notes_of(ticket_id)
    assert "review unavailable" in notes.lower(), \
        "the history does not say the review was unavailable"
    assert review.CHANGES_REQUESTED not in notes, \
        "a reviewer that could not judge was recorded as a rejection"
    assert f"{review._NOTE_PREFIX} " not in notes, \
        "a 'review:' verdict note was recorded for a review that gave no verdict"
    assert "tracker.py" in notes, "the history does not say what was missing"
    assert review.latest_verdict(ticket_id)["verdict"] == review.UNAVAILABLE


def test_a_changes_requested_reply_is_still_a_verdict_not_an_outage(db, reviewer):
    """The contrast case: the reviewer judged and said no. Unchanged."""
    reviewer["box"]["text"] = "CHANGES_REQUESTED\nThe button is still green."
    ticket_id = in_review_ticket()

    result = review.review_ticket(ticket_id, diff=DIFF, settings=Settings())

    assert result["verdict"] == review.CHANGES_REQUESTED
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW
    notes = notes_of(ticket_id)
    assert review.CHANGES_REQUESTED in notes
    assert "review unavailable" not in notes.lower()
    assert review.latest_verdict(ticket_id)["verdict"] == review.CHANGES_REQUESTED


@pytest.mark.parametrize("reply, verdict", [
    ("APPROVE\nevery criterion is met", review.APPROVE),
    ("CHANGES_REQUESTED\n- the button is red", review.CHANGES_REQUESTED),
])
def test_the_two_real_verdicts_parse_as_they_always_did(reply, verdict):
    assert review.parse_verdict(reply)[0] == verdict


def test_an_unable_to_judge_reply_parses_as_unavailable():
    verdict, reasons = review.parse_verdict(
        "UNABLE_TO_JUDGE — criterion 2 depends on eng_crew/tracker.py"
    )
    assert verdict == review.UNAVAILABLE
    assert "tracker.py" in reasons


# --- a review on its way is not reported as stopped -------------------------
#
# #100760: the run landed in in_review and the reviewer approved it 22 seconds
# later, but the Tickets page had stopped polling at in_review and kept saying
# "the review was stopped or could not run" — about a review that was running.


def test_a_review_on_its_way_is_not_called_stopped(db):
    from datetime import datetime, timedelta, timezone

    ticket_id, _ = over_budget_in_review(spent=1.0)

    assert review.awaiting_review(ticket_id) is not None
    assert review.why_unreviewed(ticket_id) is None
    later = datetime.now(timezone.utc) + timedelta(seconds=review.REVIEW_START_GRACE_S + 60)
    assert review.why_unreviewed(ticket_id, now=later) == review.NEVER_RAN


def test_a_review_the_budget_stopped_says_so(db, reviewer):
    ticket_id, run_id = over_budget_in_review()
    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(ticket_id, run_id, settings=Settings())

    assert "budget" in review.why_unreviewed(ticket_id)


def test_a_ticket_with_a_verdict_has_nothing_unreviewed(db, reviewer):
    reviewer["box"]["text"] = "CHANGES_REQUESTED\n- the button is red"
    ticket_id, _ = landed_ticket()

    assert review.why_unreviewed(ticket_id) is None


def test_the_queue_tells_a_running_review_from_a_stopped_one(db, reviewer):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew.dashboard.app import app

    running, _ = over_budget_in_review(spent=1.0)
    stopped, run_id = over_budget_in_review()
    with pytest.raises(review.ReviewUnavailable):
        review.review_ticket(stopped, run_id, settings=Settings())
    project_id = tracker.add_project("proj", "/proj", "")

    with TestClient(app) as client:
        body = client.get(f"/api/projects/{project_id}/queue").json()

    cards = {t["id"]: t for t in body["tickets"]}
    assert cards[running]["awaiting_review"] is True
    assert cards[running]["review_stopped"] is None
    assert cards[stopped]["awaiting_review"] is True
    assert "budget" in cards[stopped]["review_stopped"]
