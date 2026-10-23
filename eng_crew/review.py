"""The independent review step — what happens after a ticket's run lands.

A ticket used to stop dead at ``in_review``. The run that built it was the last
thing that ever looked at it, and nothing said whether the acceptance criteria
were actually met, so ``in_qa``, ``done`` and ``shipped`` were decorations.

This module is the reviewer: one cheap model call, given the ticket's acceptance
criteria and the run's diff, that answers a single question — are the criteria
met? — and records an approve or changes-requested verdict with reasons in the
ticket's history.

Three properties make it a review rather than a rubber stamp:

  - **It is not the builder.** A different role (``ticket_reviewer``), a
    different, cheaper model, and a credited actor of its own. A CLI provider is
    an agentic session that can edit files, and a reviewer that can edit the
    code it is judging is not a reviewer — so a CLI-only setup reviews through
    the Claude CLI with every file and shell tool disabled, the one agentic
    provider that can be run with no tools at all.
  - **It cannot substitute for the gate.** Approval only walks a ticket onward
    when the run also passed the deterministic verification gate. An approved
    ticket whose run came back ``unverified`` stays in review.
  - **It cannot ship.** ``done -> shipped`` is a human move, enforced in
    :mod:`eng_crew.tickets`.

A review that could not run is not a verdict. No API key for the configured
provider, a provider that errors, an empty reply — none of that is evidence
about the work, and recording it as ``changes_requested`` blames the build for
the reviewer's outage. The ticket stays in ``in_review`` with a history event
saying the review was unavailable and why, and a person decides what to do.

A changes-requested verdict stops there deliberately: it leaves the ticket in
``in_review`` with the reasons attached and starts no new build. Deciding to
build again is a person's call (move the ticket back to ``in_progress``), because
an auto-retry loop on an LLM verdict is how a ticket burns a budget overnight.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from . import prompts, tracing, tracker

log = logging.getLogger(__name__)

#: The agent role whose provider/model the review uses. Configure with
#: ENG_CREW_TICKET_REVIEWER_PROVIDER / _MODEL, or per stack in stacks.py.
REVIEWER_ROLE = "ticket_reviewer"

#: Who the ticket's history credits. Neither is a person — see tickets.ROBOTS.
REVIEWER_ACTOR = "reviewer"
QA_ACTOR = "qa"

APPROVE = "approve"
CHANGES_REQUESTED = "changes_requested"
#: Not a verdict: what the ticket's history records when no review could run.
UNAVAILABLE = "unavailable"

#: The word the reviewer answers with when a criterion's judgement turns
#: entirely on a file the truncated diff did not show it. It is not a third
#: verdict — it is the reviewer reporting it was not given enough to review,
#: and it lands on the same "no review happened" path as an outage.
UNABLE_TO_JUDGE_TOKEN = "UNABLE_TO_JUDGE"

#: Run statuses that mean the work is on a branch. Only "completed" also means
#: the verification gate vouched for it; "unverified" is the work without the
#: evidence.
LANDED = ("completed", "unverified")
VERIFIED = "completed"

#: Providers that drive an agentic session with file tools.
_EDITING_PROVIDERS = frozenset({"claude_cli", "gemini_cli"})

#: The Claude CLI can be launched with every tool denied, so it is the provider
#: a CLI-only setup reviews through — including when the configured CLI is a
#: different one, which has no equivalent switch.
_SANDBOX_PROVIDER = "claude_cli"
_SANDBOX_MODEL = "claude-haiku-4-5-20251001"

#: Every tool that could read or change the tree, or spawn something that can.
#: The reviewer judges the diff it was handed and nothing else.
NO_TOOLS = (
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "BashOutput",
    "KillShell", "Glob", "Grep", "Task", "Agent", "Skill", "SlashCommand",
    "ToolSearch", "WebFetch", "WebSearch", "TodoWrite",
)

#: What the sandboxed CLI call is given: no built-in tools, no MCP servers (the
#: diff is untrusted text, and the user's servers can dispatch, mail or trade),
#: and one turn — it answers.
SANDBOX_KWARGS = {"disallowed_tools": ",".join(NO_TOOLS), "no_mcp": True, "max_turns": 1}

#: The key each API provider needs. A provider with no key cannot be reached,
#: and that is an outage, not a rejection.
_API_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

#: The review is one call on a bounded prompt; a huge diff is truncated rather
#: than sent whole.
MAX_DIFF_CHARS = 30000

#: Headings for the note that says what the truncated diff left out. The
#: reviewer is told the whole shape of the change even when it cannot be shown
#: the whole change — otherwise code past the cut reads as code never written.
FILES_CHANGED_HEADING = "## Files changed on this branch"
NOT_SHOWN_HEADING = "## Files not shown to you"

#: ``diff --git a/<old> b/<new>``. The destination path is the one that matches
#: what ``git_skill.branch_files`` lists.
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<old>.+?) b/(?P<new>.+)$", re.MULTILINE)

#: Reviews are logged against the run at this subtask index. Negative, so the
#: dashboard's "subtasks done" count (which counts idx >= 0) is unaffected.
REVIEW_EVENT_IDX = -1

_FIRST_WORD = {
    "APPROVE": APPROVE,
    "APPROVED": APPROVE,
    "LGTM": APPROVE,
    "CHANGES_REQUESTED": CHANGES_REQUESTED,
    "CHANGES": CHANGES_REQUESTED,
    "REQUEST_CHANGES": CHANGES_REQUESTED,
    "REJECT": CHANGES_REQUESTED,
    "REJECTED": CHANGES_REQUESTED,
    # Only the token the prompt asks for, and the one obvious rephrasing of
    # it. Nothing looser: "unable to approve" is a rejection, not an outage.
    UNABLE_TO_JUDGE_TOKEN: UNAVAILABLE,
    "CANNOT_JUDGE": UNAVAILABLE,
}


class ReviewUnavailable(RuntimeError):
    """The review could not be run — no reviewer that is safe to use."""


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as _default
    return _default


def _missing_key(provider: str, settings: Any) -> str | None:
    """The name of the credential this provider needs and does not have."""
    var = _API_KEYS.get(provider)
    if not var:
        return None          # a CLI session or a local model: nothing to hold
    if os.environ.get(var):
        return None
    return None if getattr(settings, var.lower(), "") else var


def reviewer_config(settings: Any = None) -> dict:
    """The provider/model the review will use, and whether to sandbox the call.

    A CLI provider is rerouted to the Claude CLI with no tools rather than
    refused: a user whose only provider is the CLI should still get a review,
    and a CLI that cannot read or write anything cannot edit what it judges.
    Raises :class:`ReviewUnavailable` when the provider has no credentials.
    """
    cfg = _settings(settings)
    agent = cfg.get_agent_config(REVIEWER_ROLE)
    if agent["provider"] in _EDITING_PROVIDERS:
        model = agent["model"] if agent["provider"] == _SANDBOX_PROVIDER else _SANDBOX_MODEL
        return {"provider": _SANDBOX_PROVIDER, "model": model, "sandbox": True}
    missing = _missing_key(agent["provider"], cfg)
    if missing:
        raise ReviewUnavailable(
            f"{REVIEWER_ROLE} is configured with {agent['provider']!r} but "
            f"{missing} is not set"
        )
    return {**agent, "sandbox": False}


def run_diff(state: dict | None, *, max_chars: int = MAX_DIFF_CHARS) -> str:
    """The diff a run produced, read from the branch it committed to.

    Best effort: no git, no branch, or a worktree already swept away all mean
    the reviewer judges on what it has. It says so rather than guessing.
    """
    from . import git_skill

    branch = (state or {}).get("git_branch")
    project = (state or {}).get("main_project_path") or (state or {}).get("project_path")
    if not branch or not project:
        return ""
    try:
        return git_skill.branch_diff(project, branch, max_chars=max_chars)
    except Exception as exc:
        log.warning("could not read the diff for branch %s: %s", branch, exc)
        return ""


def changed_files(state: dict | None) -> list[str]:
    """Every file the run's branch changed — the untruncated companion to the
    diff. Best effort, for the same reasons :func:`run_diff` is."""
    from . import git_skill

    branch = (state or {}).get("git_branch")
    project = (state or {}).get("main_project_path") or (state or {}).get("project_path")
    if not branch or not project:
        return []
    try:
        return git_skill.branch_files(project, branch)
    except Exception as exc:
        log.warning("could not list the files changed on branch %s: %s", branch, exc)
        return []


def _shown_files(diff: str) -> set[str]:
    """The files whose changes the diff text carries in full.

    Truncation cuts mid-file, so the last file a truncated diff mentions has
    been shown only partly — it counts as not shown. A file the cut removed
    entirely never gets a header at all.
    """
    from . import git_skill

    headers = list(_DIFF_HEADER.finditer(diff or ""))
    if git_skill.TRUNCATION_NOTE.strip() in (diff or "") and headers:
        headers = headers[:-1]
    shown = set()
    for match in headers:
        shown.add(match.group("new"))
        shown.add(match.group("old"))
    return shown


def files_not_shown(diff: str, files: list[str] | None) -> list[str]:
    """Which of the branch's changed files the diff text does not fully carry.

    Empty when nothing was truncated: a complete diff shows everything, and a
    notice saying otherwise would be a lie the reviewer has to reason around.
    """
    from . import git_skill

    if not files or git_skill.TRUNCATION_NOTE.strip() not in (diff or ""):
        return []
    shown = _shown_files(diff)
    return [path for path in files if path not in shown]


def _file_context(diff: str, files: list[str] | None) -> str:
    """The 'here is what you were not shown' section of the review prompt."""
    missing = files_not_shown(diff, files)
    if not missing:
        return ""
    listed = "\n".join(f"- {path}" for path in (files or []))
    unshown = "\n".join(f"- {path}" for path in missing)
    return (
        f"\n{FILES_CHANGED_HEADING}\n"
        "The diff above was truncated to fit. These are all the files the "
        "branch changed:\n\n"
        f"{listed}\n"
        f"\n{NOT_SHOWN_HEADING}\n"
        "The diff text above does not contain the changes to these files, or "
        "cuts off partway through them:\n\n"
        f"{unshown}\n"
    )


def parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, reasons) from the reviewer's reply.

    Anything unrecognised is changes-requested: a reply nobody can read is not
    an approval, and the expensive direction of this mistake is approving.

    :data:`UNABLE_TO_JUDGE_TOKEN` is the one reply that is neither: it comes
    back as :data:`UNAVAILABLE`, which the caller turns into a review that
    could not run rather than a judgement on the build.
    """
    body = (text or "").strip()
    if not body:
        return CHANGES_REQUESTED, "the reviewer returned nothing"
    head, _, rest = body.partition("\n")
    # The verdict is the first word or two of the first line, however the line
    # goes on: "APPROVE", "**Changes requested**", "APPROVE — every criterion…".
    words = re.findall(r"[A-Za-z]+", head)
    verdict = None
    for count in (3, 2, 1):
        if len(words) >= count:
            verdict = _FIRST_WORD.get("_".join(words[:count]).upper())
            if verdict is not None:
                break
    trailing = re.split(r"\s[—–:-]\s|:\s", head.strip(), maxsplit=1)
    inline = trailing[1].strip() if len(trailing) > 1 else ""
    reasons = "\n".join(p for p in (inline, rest.strip()) if p) or head.strip()
    if verdict is None:
        return CHANGES_REQUESTED, f"the reviewer's verdict was unreadable: {body[:400]}"
    return verdict, reasons or "(no reasons given)"


def latest_verdict(ticket_id: int) -> dict | None:
    """The most recent recorded review of a ticket, or None."""
    from . import tickets

    for event in reversed(tickets.events(ticket_id)):
        note = event.get("note") or ""
        if event.get("actor") != REVIEWER_ACTOR:
            continue
        if note.startswith(_UNAVAILABLE_PREFIX):
            verdict = UNAVAILABLE
        elif note.startswith(_NOTE_PREFIX):
            verdict = CHANGES_REQUESTED if CHANGES_REQUESTED in note else APPROVE
        else:
            continue
        return {
            "verdict": verdict,
            "reasons": note.split(_UNAVAILABLE_PREFIX)[-1].split("—", 1)[-1].strip(),
            "at": event.get("created_at"),
            "actor": event.get("actor"),
        }
    return None


_NOTE_PREFIX = "review:"
_UNAVAILABLE_PREFIX = "review unavailable:"


def _unavailable(ticket_id: int, reason: str) -> ReviewUnavailable:
    """Record that no review happened, and why; hand back what to raise.

    The ticket stays where it is. Nothing about the build was judged, so there
    is no verdict to record — only the fact that nobody looked.
    """
    from . import tickets

    try:
        tickets.note(ticket_id, REVIEWER_ACTOR, f"{_UNAVAILABLE_PREFIX} {reason}".strip())
    except Exception:
        log.exception("could not record the unavailable review of ticket %s", ticket_id)
    return ReviewUnavailable(reason)


def _note(verdict: str, reasons: str) -> str:
    return f"{_NOTE_PREFIX} {verdict} — {reasons}".strip()


def review_ticket(
    ticket_id: int,
    run_id: int | None = None,
    *,
    diff: str = "",
    files: list[str] | None = None,
    settings: Any = None,
) -> dict | None:
    """Review a ticket's landed run and move the ticket accordingly.

    Returns the verdict as ``{verdict, reasons, status}``, or None when there is
    nothing to review (no ticket, or a ticket that is not awaiting review).
    Raises :class:`ReviewUnavailable` if no reviewer can safely be used.
    """
    from . import tickets

    cfg = _settings(settings)
    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        log.warning("review_ticket: no ticket %s", ticket_id)
        return None
    if ticket.get("status") != tickets.IN_REVIEW:
        log.info(
            "review_ticket: ticket %s is %s, not awaiting review",
            ticket_id, ticket.get("status"),
        )
        return None
    if not getattr(cfg, "ticket_review_enabled", True):
        raise ReviewUnavailable("ticket review is disabled (ENG_CREW_TICKET_REVIEW_ENABLED)")

    # The review is cheap, but it is still a paid step, and a ticket that has
    # already spent its authority does not get to spend more on its own say-so.
    # The build it would have judged is safe on its branch either way.
    from . import budget

    try:
        budget.check(ticket_id, "the review", settings=cfg)
    except budget.BudgetExhausted as exc:
        raise ReviewUnavailable(str(exc)) from exc

    run = tracker.get_run_detail(run_id) if run_id else tracker.get_ticket_run(ticket_id)
    run_status = (run or {}).get("status") or "unknown"
    run_id = run_id or (run or {}).get("id") or 0

    # Resolving a reviewer and reaching it are the two ways a review simply does
    # not happen. Either way the ticket's history says so, and nothing is judged.
    try:
        reviewer = reviewer_config(cfg)
        verdict, reasons = _ask(
            ticket, run, diff, run_id=run_id, settings=cfg, reviewer=reviewer,
            files=files if files is not None else changed_files(run),
        )
    except ReviewUnavailable as exc:
        raise _unavailable(ticket_id, str(exc)) from exc
    if run_id:
        tracker.recalculate_run_cost(run_id)

    tickets.note(ticket_id, REVIEWER_ACTOR, _note(verdict, reasons))

    status = tickets.IN_REVIEW
    if verdict == APPROVE and run_status == VERIFIED:
        status = _advance(ticket_id, reasons)
    elif verdict == APPROVE:
        tickets.note(
            ticket_id, REVIEWER_ACTOR,
            f"approved, but the run finished as {run_status} — it did not pass the "
            f"verification gate, so the ticket stays in review",
        )

    return {"verdict": verdict, "reasons": reasons, "status": status}


def _ask(ticket: dict, run: dict | None, diff: str, *, run_id: int,
         settings: Any, reviewer: dict,
         files: list[str] | None = None) -> tuple[str, str]:
    """The one model call. A reviewer that cannot be reached returns no verdict."""
    from .agents.base import BaseAgent

    prompt = prompts.render(
        "review-ticket",
        title=str(ticket.get("title") or "").strip(),
        description=str(ticket.get("description") or "").strip(),
        criteria=str(ticket.get("acceptance_criteria") or "").strip()
        or "(none recorded — say so and request changes)",
        verification=_verification_note(run),
        diff=(diff or "").strip() or "(the diff could not be read)",
        file_context=_file_context(diff, files),
    )
    # The review is a CLI call like any other, so its tool calls are traced
    # against the run it judges — a sandboxed reviewer that somehow reached a
    # tool is exactly the thing worth being able to see afterwards.
    extra = dict(SANDBOX_KWARGS) if reviewer.get("sandbox") else {}
    extra["on_event"] = tracing.run_event_handler(run_id, subtask_idx=REVIEW_EVENT_IDX)
    try:
        result = BaseAgent(settings).call(
            prompt, role=REVIEWER_ROLE, run_id=run_id, subtask_idx=REVIEW_EVENT_IDX,
            override={"provider": reviewer["provider"], "model": reviewer["model"]},
            extra=extra,
        )
    except Exception as exc:
        log.warning("the reviewer could not be reached: %s", exc)
        raise ReviewUnavailable(f"the reviewer could not be reached: {exc}") from exc
    if not (result.text or "").strip():
        raise ReviewUnavailable("the reviewer returned nothing")
    verdict, reasons = parse_verdict(result.text)
    if verdict == UNAVAILABLE:
        # The reviewer read the criteria and said it was not shown the code one
        # of them turns on. That is the same fact as an outage — nobody judged
        # the work — so it takes the same path and leaves no verdict behind.
        raise ReviewUnavailable(
            f"the reviewer could not judge the criteria from the diff it was "
            f"shown: {reasons}"
        )
    return verdict, reasons


def _verification_note(run: dict | None) -> str:
    status = (run or {}).get("status") or "unknown"
    if status == VERIFIED:
        return "The run passed the project's tests and build."
    if status == "unverified":
        return "The run produced no verifiable evidence — treat its tests as unproven."
    return f"The run finished as {status}."


def _advance(ticket_id: int, reasons: str) -> str:
    """in_review -> in_qa -> done, each move credited to who made it.

    QA here is the verification gate that already ran: the tests and build
    passed on this branch, which is the check a QA pass consists of. Crediting
    it separately keeps the history honest about which step said what.
    """
    from . import tickets

    for to_status, actor, note in (
        (tickets.IN_QA, REVIEWER_ACTOR, f"{_NOTE_PREFIX} approved — {reasons}"),
        (tickets.DONE, QA_ACTOR, "verification gate passed on the run's branch"),
    ):
        try:
            tickets.move(ticket_id, to_status, actor, note)
        except tickets.TransitionRefused as exc:
            log.warning("ticket %s stopped before %s: %s", ticket_id, to_status, exc)
            break
    return (tracker.get_backlog_item(ticket_id) or {}).get("status") or tickets.IN_REVIEW
