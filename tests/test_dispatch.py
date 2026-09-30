"""Dispatching a free-text task goes through a ticket.

`run_task` used to fork straight into the pipeline, which meant its builds got
none of what a ticket's build gets — no acceptance criteria to review against,
no budget cap, no retro, and no WIP limit bounding how many a chat window could
start. What is worth pinning down is therefore not the subprocess but the route:
a ticket exists, it has criteria (given or drafted), the dispatch is what
readied it, the limit refuses rather than warns, and the build that is launched
is the ticket's build with the base branch intact.
"""
from __future__ import annotations

import pytest

from eng_crew import dispatch, git_skill, manager, tickets, tracker
from eng_crew.config import Settings


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "dispatch.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    return root


@pytest.fixture
def launched(monkeypatch):
    """Capture the build command instead of running it. Set .fail to break it."""

    class Recorder:
        def __init__(self):
            self.calls: list[list[str]] = []
            self.fail = False

        def __call__(self, cmd, **kwargs):
            if self.fail:
                raise OSError("no such executable")
            self.calls.append(list(cmd))
            self.kwargs = kwargs
            return object()

        @property
        def cmd(self) -> list[str]:
            assert self.calls, "no build was launched"
            return self.calls[-1]

    rec = Recorder()
    monkeypatch.setattr(dispatch.subprocess, "Popen", rec)
    return rec


@pytest.fixture
def drafter(monkeypatch):
    """The Manager's drafting call, stubbed. Set .proposal or .error."""

    class Drafter:
        proposal = {
            "title": "Add pagination to the runs table",
            "description": "20 per page, filters preserved",
            "acceptance_criteria": ["the table shows 20 rows", "filters survive paging"],
        }
        error = ""

        def __call__(self, task, project_path=".", project_context=""):
            self.task = task
            self.project_path = project_path
            if self.error:
                raise manager.DraftFailed(self.error)
            return self.proposal

    d = Drafter()
    monkeypatch.setattr(manager, "draft_ticket", d)
    return d


def cfg(**kw) -> Settings:
    return Settings(**kw)


def history(ticket_id: int) -> list[tuple[str, str]]:
    return [(e["to_status"], e["actor"]) for e in tickets.events(ticket_id)]


# --- the task becomes a ticket ------------------------------------------


def test_a_dispatch_files_a_ticket_and_builds_it(db, project, launched, drafter):
    result = dispatch.dispatch_task(
        str(project), "Add pagination to the runs table",
        "the table shows 20 rows\nfilters survive paging", settings=cfg(),
    )

    assert result.ok and result.started
    ticket = tracker.get_backlog_item(result.ticket_id)
    assert ticket["status"] == tickets.IN_PROGRESS
    assert "20 rows" in ticket["acceptance_criteria"]
    assert ticket["project_path"] == str(project.resolve())


def test_given_criteria_are_used_without_asking_the_manager(db, project, launched, drafter):
    result = dispatch.dispatch_task(
        str(project), "Add pagination", "- one page is 20 rows", settings=cfg()
    )
    assert result.criteria_source == "given"
    assert not hasattr(drafter, "task"), "the manager was asked for criteria it did not need"


def test_the_manager_drafts_criteria_when_none_are_given(db, project, launched, drafter):
    result = dispatch.dispatch_task(
        str(project), "Add pagination to the runs table", settings=cfg()
    )

    assert result.criteria_source == "manager"
    assert drafter.project_path == str(project.resolve()), "the draft was not grounded in the project"
    ticket = tracker.get_backlog_item(result.ticket_id)
    assert "the table shows 20 rows" in ticket["acceptance_criteria"]
    assert "filters survive paging" in ticket["acceptance_criteria"]
    # A drafted ticket is the manager's writing, so its history says so.
    assert (tickets.BACKLOG, "manager") in history(result.ticket_id)


def test_a_ticket_nobody_can_judge_is_not_filed(db, project, launched, drafter):
    drafter.error = "the manager drafted no acceptance criteria for this task"

    result = dispatch.dispatch_task(str(project), "make it better", settings=cfg())

    assert not result.ok
    assert "acceptance_criteria" in result.detail
    assert tracker.list_project_tickets(str(project.resolve())) == []
    assert launched.calls == []


# --- the dispatch is the approval --------------------------------------


def test_dispatching_is_what_readies_the_ticket(db, project, launched, drafter):
    result = dispatch.dispatch_task(
        str(project), "Add pagination", "- 20 rows", actor="alway", settings=cfg()
    )
    # backlog (filed) -> ready (the human's approval) -> in_progress (the build)
    assert history(result.ticket_id) == [
        (tickets.BACKLOG, "alway"),
        (tickets.READY, "alway"),
        (tickets.IN_PROGRESS, "alway"),
    ]


def test_the_build_is_the_tickets_build(db, project, launched, drafter):
    result = dispatch.dispatch_task(str(project), "Add pagination", "- 20 rows", settings=cfg())
    cmd = launched.cmd
    assert cmd[2:6] == ["-m", "eng_crew", "ticket", str(result.ticket_id)]
    assert "--no-approval" in cmd


def test_base_branch_reaches_the_tickets_run(db, project, launched, drafter, monkeypatch):
    monkeypatch.setattr(git_skill, "branch_exists", lambda path, branch: True)

    dispatch.dispatch_task(
        str(project), "Add pagination", "- 20 rows",
        base_branch="ai-team/earlier-run", settings=cfg(),
    )
    cmd = launched.cmd
    assert cmd[cmd.index("--base") + 1] == "ai-team/earlier-run"


def test_a_base_branch_that_does_not_exist_files_nothing(db, project, launched, drafter, monkeypatch):
    monkeypatch.setattr(git_skill, "branch_exists", lambda path, branch: False)

    result = dispatch.dispatch_task(
        str(project), "Add pagination", "- 20 rows", base_branch="nope", settings=cfg()
    )

    assert not result.ok and "nope" in result.detail
    assert tracker.list_project_tickets(str(project.resolve())) == []


# --- the limits apply ---------------------------------------------------


def test_over_the_wip_limit_the_ticket_waits_instead_of_building(db, project, launched, drafter):
    path = str(project.resolve())
    held = tracker.add_backlog_item("held", "", path, "", acceptance_criteria="- yes")
    tickets.move(held, tickets.READY, "tester")
    tickets.move(held, tickets.IN_PROGRESS, "tester", settings=cfg(wip_limit=1))

    result = dispatch.dispatch_task(
        str(project), "Add pagination", "- 20 rows", settings=cfg(wip_limit=1)
    )

    assert result.ok and not result.started
    assert "WIP limit reached" in result.detail
    assert launched.calls == []
    # The dispatch is not lost: the ticket is ready, and the queue can pull it.
    assert tracker.get_backlog_item(result.ticket_id)["status"] == tickets.READY


def test_a_launch_that_fails_gives_the_wip_slot_back(db, project, launched, drafter):
    launched.fail = True

    result = dispatch.dispatch_task(str(project), "Add pagination", "- 20 rows", settings=cfg())

    assert not result.ok
    assert tracker.get_backlog_item(result.ticket_id)["status"] == tickets.BACKLOG
    assert tickets.wip(str(project.resolve()), cfg())["in_flight"] == 0


def test_a_dispatch_needs_a_real_project(db, launched, drafter):
    result = dispatch.dispatch_task("/no/such/project", "Add pagination", "- 20 rows")
    assert not result.ok and "not a directory" in result.detail


def test_an_empty_task_is_refused(db, project, launched, drafter):
    result = dispatch.dispatch_task(str(project), "   ", "- 20 rows", settings=cfg())
    assert not result.ok and "task" in result.detail


# --- the manager's drafting seam ---------------------------------------


def test_draft_ticket_refuses_a_reply_with_no_criteria(monkeypatch):
    import json

    from eng_crew.providers.base import LLMResult

    block = "```ticket\n" + json.dumps({"title": "Do the thing"}) + "\n```"
    monkeypatch.setattr(manager, "call_llm", lambda *a, **k: LLMResult(text=block))

    with pytest.raises(manager.DraftFailed, match="acceptance criteria"):
        manager.draft_ticket("do the thing", ".")


def test_draft_ticket_returns_a_proposal_with_criteria(monkeypatch):
    import json

    from eng_crew.providers.base import LLMResult

    block = "```ticket\n" + json.dumps({
        "title": "Do the thing",
        "description": "the details",
        "acceptance_criteria": ["it works"],
    }) + "\n```"
    seen = {}

    def fake(provider, model, prompt, **kwargs):
        seen.update(provider=provider, tools=kwargs.get("allowed_tools"), cwd=kwargs.get("cwd"))
        return LLMResult(text=block)

    monkeypatch.setattr(manager, "call_llm", fake)

    proposal = manager.draft_ticket("do the thing", "/proj")

    assert proposal["acceptance_criteria"] == ["it works"]
    # Grounded and read-only: it reads the project it is drafting against.
    assert seen["cwd"] == "/proj"
    assert seen["tools"] == manager.MANAGER_TOOLS


def test_draft_ticket_with_no_task_asks_no_model():
    with pytest.raises(manager.DraftFailed):
        manager.draft_ticket("   ", ".")


# --- the ticket's run keeps the base branch ----------------------------


def test_run_ticket_bases_the_run_where_it_was_told(db, monkeypatch):
    """The MCP tool's `base_branch` has to survive the trip through the ticket."""
    from eng_crew import run as run_mod

    ticket_id = tracker.add_backlog_item(
        "do a thing", "the details", "/proj", "", acceptance_criteria="- it works"
    )
    tickets.move(ticket_id, tickets.READY, "tester")

    seen = {}

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        seen["base_branch"] = base_branch
        tracker.finish_run(run_id, status="completed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(run_mod, "_review_ticket", lambda *a, **k: None)

    run_mod.run_ticket(ticket_id, actor="tester", base_branch="ai-team/earlier-run")

    assert seen["base_branch"] == "ai-team/earlier-run"
