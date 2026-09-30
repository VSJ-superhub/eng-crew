"""The ticket lifecycle — eng-crew's unit of work.

A ticket is a row in ``backlog_items`` that is not a subtask. It carries a title,
a description, acceptance criteria, and a status that moves along a fixed
lifecycle:

    backlog -> ready -> in_progress -> in_review -> in_qa -> done -> shipped
                                                        \\-> rejected -/

Three rules make the lifecycle worth having rather than decorative:

  - **Acceptance criteria gate.** A ticket cannot reach ``ready`` without
    acceptance criteria. "Ready" means an agent could pick it up and know when
    it is finished; a ticket with no definition of done is not that.
  - **WIP limit.** There is no sprint. Work is a continuous queue, and the only
    thing bounding how much is in flight at once is the limit checked here
    (``ENG_CREW_WIP_LIMIT``). Nothing enters ``in_progress`` past the limit.
  - **Retro on ship.** Reaching ``shipped`` writes the ticket's history into the
    lessons store (:mod:`eng_crew.retro`), where the next ticket's builder will
    read it. A lifecycle whose end produces nothing is a queue, not a process.

Every accepted move is written to ``ticket_events`` with the actor who made it,
so a ticket's history is auditable — who moved it, when, and why. The rules live
here; the SQL lives in :mod:`eng_crew.tracker`.
"""
from __future__ import annotations

from typing import Any, Iterable

# tracker imports the status constants below during its schema migration, so the
# dependency has to run one way at import time: the SQL is fetched per call.

BACKLOG = "backlog"
READY = "ready"
IN_PROGRESS = "in_progress"
IN_REVIEW = "in_review"
IN_QA = "in_qa"
DONE = "done"
REJECTED = "rejected"
SHIPPED = "shipped"

STATUSES: tuple[str, ...] = (
    BACKLOG, READY, IN_PROGRESS, IN_REVIEW, IN_QA, DONE, REJECTED, SHIPPED,
)

#: Statuses that consume a WIP slot — a ticket someone (or some run) is holding.
IN_FLIGHT: tuple[str, ...] = (IN_PROGRESS, IN_REVIEW, IN_QA)

TRANSITIONS: dict[str, frozenset[str]] = {
    BACKLOG:     frozenset({READY}),
    READY:       frozenset({IN_PROGRESS, BACKLOG}),
    IN_PROGRESS: frozenset({IN_REVIEW, REJECTED, BACKLOG}),
    IN_REVIEW:   frozenset({IN_QA, IN_PROGRESS, REJECTED}),
    IN_QA:       frozenset({DONE, REJECTED}),
    DONE:        frozenset({SHIPPED}),
    REJECTED:    frozenset({BACKLOG, READY}),
    SHIPPED:     frozenset(),
}

#: How pre-lifecycle rows map onto the lifecycle, used by the schema migration
#: and by anything still handing eng-crew a legacy status string.
LEGACY_STATUS = {
    "pending": BACKLOG,
    "running": IN_PROGRESS,
    "failed": REJECTED,
    "completed": DONE,
    "done": DONE,
    # Shelved work: nobody is building it, but it can be picked back up.
    "archived": REJECTED,
}


#: Actors that are eng-crew itself rather than a person. They may move a ticket
#: anywhere the lifecycle allows except into `shipped`: shipping is the one step
#: that says "this is live for users", and no agent gets to decide that.
ROBOTS: frozenset[str] = frozenset({
    "queue", "cleanup", "system", "migration", "planner", "manager",
    "architect", "builder", "reviewer", "qa", "budget", "retro",
})


class TransitionRefused(ValueError):
    """A status change the lifecycle does not allow."""


def allowed_transitions(status: str) -> frozenset[str]:
    return TRANSITIONS.get(status, frozenset())


def wip_limit(settings: Any = None) -> int:
    if settings is None:
        from .config import settings as _default_settings
        settings = _default_settings
    return max(1, int(getattr(settings, "wip_limit", 3)))


def _tracker():
    from . import tracker
    return tracker


def wip(project_path: str, settings: Any = None) -> dict:
    """WIP accounting for one project: the limit, what is in flight, free slots."""
    limit = wip_limit(settings)
    in_flight = _tracker().count_tickets_in_flight(project_path)
    return {
        "limit": limit,
        "in_flight": in_flight,
        "slots": max(0, limit - in_flight),
        "statuses": list(IN_FLIGHT),
    }


def queue(project_path: str, limit: int | None = None) -> list[dict]:
    """The continuous queue: tickets in ``ready``, highest priority first."""
    return _tracker().list_ready_tickets(project_path, limit=limit)


def move(ticket_id: int, to_status: str, actor: str, note: str = "",
         *, settings: Any = None) -> dict:
    """Move a ticket, recording who did it. Raises TransitionRefused if illegal.

    Returns the updated ticket.
    """
    tracker = _tracker()
    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        raise TransitionRefused(f"no ticket {ticket_id}")
    if to_status not in STATUSES:
        raise TransitionRefused(f"{to_status!r} is not a ticket status")

    from_status = ticket.get("status") or BACKLOG
    if to_status == from_status:
        raise TransitionRefused(f"ticket {ticket_id} is already {to_status}")
    if to_status not in allowed_transitions(from_status):
        allowed = ", ".join(sorted(allowed_transitions(from_status))) or "nothing"
        raise TransitionRefused(
            f"a ticket in {from_status} can only move to {allowed}, not {to_status}"
        )

    if to_status == SHIPPED and (actor or "system").strip().lower() in ROBOTS:
        raise TransitionRefused(
            f"only a person can ship ticket {ticket_id} — {actor!r} is eng-crew itself"
        )

    if to_status == READY and not (ticket.get("acceptance_criteria") or "").strip():
        raise TransitionRefused(
            f"ticket {ticket_id} has no acceptance criteria — it cannot be ready"
        )

    if to_status == IN_PROGRESS:
        # Entering in_progress is a build about to start, which is the first
        # place a ticket spends money. Spend authority is checked here so every
        # surface that can launch one — the queue, the dashboard, the CLI —
        # is bound by it without each having to remember.
        from . import budget
        budget.check(ticket_id, "a build", settings=settings)

    if to_status == IN_PROGRESS and from_status not in IN_FLIGHT:
        state = wip(ticket.get("project_path") or "", settings)
        if state["slots"] <= 0:
            raise TransitionRefused(
                f"WIP limit reached ({state['in_flight']}/{state['limit']} in flight) — "
                f"finish something before starting ticket {ticket_id}"
            )

    if not tracker.set_ticket_status(ticket_id, from_status, to_status, actor, note):
        raise TransitionRefused(
            f"ticket {ticket_id} changed status underneath us; it is no longer {from_status}"
        )

    if to_status == SHIPPED:
        # Shipping is the last thing that happens to a ticket, which makes it
        # the only moment its history is complete. The retro is written here,
        # next to the budget check and for the same reason: every surface that
        # can ship one goes through this function. It never raises — the ticket
        # has already shipped.
        from . import retro
        retro.on_shipped(ticket_id, settings)

    return tracker.get_backlog_item(ticket_id) or ticket


def note(ticket_id: int, actor: str, text: str) -> bool:
    """Record something about a ticket without moving it.

    A review verdict belongs in the same history as the status changes — it is
    the reason the next move happened, or the reason none did — but a
    changes-requested verdict moves the ticket nowhere. This writes the event
    with the ticket's current status on both sides.
    """
    ticket = _tracker().get_backlog_item(ticket_id)
    if not ticket:
        return False
    status = ticket.get("status") or BACKLOG
    return _tracker().add_ticket_event(ticket_id, status, status, actor, text)


def events(ticket_id: int) -> list[dict]:
    return _tracker().get_ticket_events(ticket_id)


# The heading task_text writes above a ticket's criteria, and the marker
# acceptance_criteria_of() reads them back out of a task string. An execution
# tier only ever sees the flat task, so this is the one channel by which it can
# learn that the work has a definition of done.
CRITERIA_HEADING = "Acceptance criteria:"


def task_text(ticket: dict) -> str:
    """The build instruction for a ticket — what a run is actually asked to do.

    This is what replaced "Execute sprint N": the agent gets the ticket's own
    title, description, and acceptance criteria, so it can tell when it is done.
    """
    parts = [str(ticket.get("title") or "").strip()]
    description = str(ticket.get("description") or "").strip()
    if description and description != parts[0]:
        parts.append(description)
    criteria = str(ticket.get("acceptance_criteria") or "").strip()
    if criteria:
        parts.append(f"{CRITERIA_HEADING}\n" + criteria)
    return "\n\n".join(p for p in parts if p)


def acceptance_criteria_of(task: str) -> str:
    """The acceptance criteria carried by a task string, or "".

    The inverse of :func:`task_text`, for the execution tiers: they are handed a
    flat task with no ticket to consult, and "does this work have a definition of
    done?" changes how they should go about it.
    """
    text = str(task or "")
    index = text.lower().rfind(CRITERIA_HEADING.lower())
    if index == -1:
        return ""
    return text[index + len(CRITERIA_HEADING):].strip()


def summarize(tickets: Iterable[dict]) -> dict[str, int]:
    """Count tickets per status, every status present even at zero."""
    counts = {status: 0 for status in STATUSES}
    for ticket in tickets:
        status = ticket.get("status") or BACKLOG
        counts[status] = counts.get(status, 0) + 1
    return counts
