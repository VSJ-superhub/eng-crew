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
    different, cheaper model, and a credited actor of its own. It runs on a
    plain API provider, never a CLI one: a CLI provider is an agentic session
    that can edit files, and a reviewer that can edit the code it is judging is
    not a reviewer. That is refused rather than downgraded silently.
  - **It cannot substitute for the gate.** Approval only walks a ticket onward
    when the run also passed the deterministic verification gate. An approved
    ticket whose run came back ``unverified`` stays in review.
  - **It cannot ship.** ``done -> shipped`` is a human move, enforced in
    :mod:`eng_crew.tickets`.

A changes-requested verdict stops there deliberately: it leaves the ticket in
``in_review`` with the reasons attached and starts no new build. Deciding to
build again is a person's call (move the ticket back to ``in_progress``), because
an auto-retry loop on an LLM verdict is how a ticket burns a budget overnight.
"""
from __future__ import annotations

import logging
from typing import Any

from . import prompts, tracker

log = logging.getLogger(__name__)

#: The agent role whose provider/model the review uses. Configure with
#: ENG_CREW_TICKET_REVIEWER_PROVIDER / _MODEL, or per stack in stacks.py.
REVIEWER_ROLE = "ticket_reviewer"

#: Who the ticket's history credits. Neither is a person — see tickets.ROBOTS.
REVIEWER_ACTOR = "reviewer"
QA_ACTOR = "qa"

APPROVE = "approve"
CHANGES_REQUESTED = "changes_requested"

#: Run statuses that mean the work is on a branch. Only "completed" also means
#: the verification gate vouched for it; "unverified" is the work without the
#: evidence.
LANDED = ("completed", "unverified")
VERIFIED = "completed"

#: Providers that drive an agentic session with file tools.
_EDITING_PROVIDERS = frozenset({"claude_cli", "gemini_cli"})

#: The review is one call on a bounded prompt; a huge diff is truncated rather
#: than sent whole.
MAX_DIFF_CHARS = 30000

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
}


class ReviewUnavailable(RuntimeError):
    """The review could not be run — no reviewer that is safe to use."""


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as _default
    return _default


def reviewer_config(settings: Any = None) -> dict:
    """The provider/model the review will use. Raises if it could edit files."""
    cfg = _settings(settings).get_agent_config(REVIEWER_ROLE)
    if cfg["provider"] in _EDITING_PROVIDERS:
        raise ReviewUnavailable(
            f"{REVIEWER_ROLE} is configured with {cfg['provider']!r}, an agentic "
            "provider that can edit files — a reviewer must not be able to change "
            "the code it is judging"
        )
    return cfg


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


def parse_verdict(text: str) -> tuple[str, str]:
    """(verdict, reasons) from the reviewer's reply.

    Anything unrecognised is changes-requested: a reply nobody can read is not
    an approval, and the expensive direction of this mistake is approving.
    """
    body = (text or "").strip()
    if not body:
        return CHANGES_REQUESTED, "the reviewer returned nothing"
    head, _, rest = body.partition("\n")
    token = head.strip().strip("*#:.-").replace(" ", "_").upper()
    verdict = _FIRST_WORD.get(token)
    reasons = rest.strip() or head.strip()
    if verdict is None:
        return CHANGES_REQUESTED, f"the reviewer's verdict was unreadable: {body[:400]}"
    return verdict, reasons or "(no reasons given)"


def latest_verdict(ticket_id: int) -> dict | None:
    """The most recent recorded review of a ticket, or None."""
    from . import tickets

    for event in reversed(tickets.events(ticket_id)):
        note = event.get("note") or ""
        if event.get("actor") == REVIEWER_ACTOR and note.startswith(_NOTE_PREFIX):
            verdict = CHANGES_REQUESTED if CHANGES_REQUESTED in note else APPROVE
            return {
                "verdict": verdict,
                "reasons": note.split("—", 1)[-1].strip(),
                "at": event.get("created_at"),
                "actor": event.get("actor"),
            }
    return None


_NOTE_PREFIX = "review:"


def _note(verdict: str, reasons: str) -> str:
    return f"{_NOTE_PREFIX} {verdict} — {reasons}".strip()


def review_ticket(
    ticket_id: int,
    run_id: int | None = None,
    *,
    diff: str = "",
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

    # Resolve (and vet) the reviewer before spending anything.
    reviewer_config(cfg)

    run = tracker.get_run_detail(run_id) if run_id else tracker.get_ticket_run(ticket_id)
    run_status = (run or {}).get("status") or "unknown"
    run_id = run_id or (run or {}).get("id") or 0

    verdict, reasons = _ask(ticket, run, diff, run_id=run_id, settings=cfg)
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
         settings: Any) -> tuple[str, str]:
    """The one model call. A reviewer that cannot be reached does not approve."""
    from .agents.base import BaseAgent

    prompt = prompts.render(
        "review-ticket",
        title=str(ticket.get("title") or "").strip(),
        description=str(ticket.get("description") or "").strip(),
        criteria=str(ticket.get("acceptance_criteria") or "").strip()
        or "(none recorded — say so and request changes)",
        verification=_verification_note(run),
        diff=(diff or "").strip() or "(the diff could not be read)",
    )
    try:
        result = BaseAgent(settings).call(
            prompt, role=REVIEWER_ROLE, run_id=run_id, subtask_idx=REVIEW_EVENT_IDX,
        )
    except Exception as exc:
        log.warning("the reviewer could not be reached: %s", exc)
        return CHANGES_REQUESTED, f"the reviewer could not be reached: {exc}"
    return parse_verdict(result.text)


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
