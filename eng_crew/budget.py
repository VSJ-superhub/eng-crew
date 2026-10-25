"""Spend authority on a ticket — the money side of the ticket lifecycle.

A ticket is the unit of work, and it was also an unbounded line of credit: a
run that went badly could be repaired, reviewed and rebuilt with nothing
counting the cost. This module gives a ticket the thing spend authority gives a
person in a company — an amount it may spend, below which work just proceeds,
and above which work stops and goes to a human instead of spending more.

Two properties make it authority rather than a warning:

  - **It stops rather than reports.** Crossing the budget refuses the next paid
    step and records why in the ticket's history. Nothing retries it; the
    ticket sits where a person will see it, and raising the budget (or closing
    the ticket) is their call.
  - **It is checked where a step *starts*.** A model call cannot be interrupted
    mid-flight, so there is no point pretending the budget is a hard ceiling on
    the cent. The step that is already running is allowed to finish and is
    charged in full; what the budget governs is whether the *next* one begins.
    The enforcement points are therefore: entering ``in_progress`` (a build),
    and the two paid steps that happen after a build without a new
    transition — the repair pass in :mod:`eng_crew.agents.verifier` and the
    review in :mod:`eng_crew.review`. A repair pass is the one step that can
    also be bounded while it runs: its session is capped at what the ticket
    has left, so it cannot carry the ticket far past its budget.

A run with no ticket is outside all of this. There is nothing to charge it to
and nobody asked for authority over it, so it behaves exactly as it always did.
"""
from __future__ import annotations

import logging
from typing import Any

from . import tracker
from .tickets import TransitionRefused

log = logging.getLogger(__name__)

#: Fallback when nothing is configured. ENG_CREW_TICKET_BUDGET_USD overrides it,
#: and a ticket's own ``budget_usd`` overrides that.
DEFAULT_BUDGET_USD = 15.0

#: Who the ticket's history credits for a budget stop. Not a person — see
#: tickets.ROBOTS — which is the point: it escalates *to* one.
ACTOR = "budget"

#: Every note this module writes starts here, so the history is greppable.
NOTE_PREFIX = "budget:"

#: The sentence a stopped ticket's history ends on. A ticket stopped for budget
#: is not failed and not retryable — it is waiting on someone with authority.
ESCALATION = "needs a human decision: raise the budget or close the ticket"


class BudgetExhausted(TransitionRefused):
    """A paid step refused because the ticket has spent its budget.

    A subclass of :class:`~eng_crew.tickets.TransitionRefused` so every caller
    that already handles a refused move — the CLI, the dashboard, the queue —
    reports this the same way, with its own reason.
    """

    def __init__(self, ticket_id: int, step: str, spent: float, limit: float):
        self.ticket_id = ticket_id
        self.step = step
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"ticket {ticket_id} has spent ${spent:.2f} of its ${limit:.2f} budget — "
            f"{step} was not started; {ESCALATION}"
        )


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as _default
    return _default


def budget_for(ticket: dict, settings: Any = None) -> float:
    """What this ticket may spend: its own budget, else the configured default."""
    own = (ticket or {}).get("budget_usd")
    if own is not None:
        return float(own)
    return float(getattr(_settings(settings), "ticket_budget_usd", DEFAULT_BUDGET_USD))


def status(ticket_id: int | None, settings: Any = None) -> dict | None:
    """A ticket's budget and what it has spent against it, or None if no ticket."""
    if not ticket_id:
        return None
    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        return None
    limit = budget_for(ticket, settings)
    spent = tracker.ticket_spend(ticket_id)
    return {
        "ticket_id": ticket_id,
        "budget_usd": limit,
        "spent_usd": spent,
        "remaining_usd": limit - spent,
        "exhausted": spent >= limit,
    }


def status_for_run(run_id: int, settings: Any = None) -> dict | None:
    """The budget governing a run, or None when the run has no ticket."""
    return status(tracker.get_run_ticket_id(run_id), settings)


def annotate(ticket: dict, settings: Any = None) -> dict:
    """A ticket dict with its spend alongside its budget, for a display surface."""
    limit = budget_for(ticket, settings)
    spent = tracker.ticket_spend(ticket["id"])
    return {
        **ticket,
        "budget_usd": limit,
        "spent_usd": spent,
        "budget_exhausted": spent >= limit,
    }


def check(ticket_id: int | None, step: str, settings: Any = None) -> dict | None:
    """Authorise the next paid step on a ticket.

    Returns the budget status (None for a ticket-less run, which is never
    checked). Raises :class:`BudgetExhausted` — after recording the stop in the
    ticket's history — when the budget is already spent.
    """
    state = status(ticket_id, settings)
    if state is None or not state["exhausted"]:
        return state
    _record_stop(ticket_id, step, state)
    raise BudgetExhausted(ticket_id, step, state["spent_usd"], state["budget_usd"])


def allows(ticket_id: int | None, step: str, settings: Any = None) -> bool:
    """check() for a caller that must not raise — the verification gate is one.

    True when the step may start. False records the stop first, so the ticket's
    history explains the pass that never happened.
    """
    try:
        check(ticket_id, step, settings)
    except BudgetExhausted as exc:
        log.warning("%s", exc)
        return False
    return True


def allows_for_run(run_id: int, step: str, settings: Any = None) -> bool:
    """allows() for a caller that knows the run but not the ticket."""
    ticket_id = tracker.get_run_ticket_id(run_id)
    if not ticket_id:
        return True
    return allows(ticket_id, step, settings)


def set_budget(ticket_id: int, amount: float, actor: str = "user",
               settings: Any = None) -> dict | None:
    """Change a ticket's budget, recording who did it.

    Raising it is what releases a ticket stopped for budget: the next paid step
    checks the new figure and proceeds. Nothing is restarted here — a person
    decided to authorise more spend, not to launch a build. A review the budget
    stopped is the surfaces' to restart (:func:`eng_crew.review.review_if_waiting`),
    since it is a model call and this is a bookkeeping change.
    """
    from . import tickets

    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        return None
    was = budget_for(ticket, settings)
    new = float(amount)
    tracker.update_backlog_item(ticket_id, budget_usd=new)
    tickets.note(
        ticket_id, actor,
        f"{NOTE_PREFIX} budget changed from ${was:.2f} to ${new:.2f}",
    )
    return status(ticket_id, settings)


def _record_stop(ticket_id: int, step: str, state: dict) -> None:
    """Write the stop into the ticket's history, once per stopped step."""
    from . import tickets

    tickets.note(
        ticket_id, ACTOR,
        f"{NOTE_PREFIX} stopped before {step} — ${state['spent_usd']:.2f} spent of a "
        f"${state['budget_usd']:.2f} budget; {ESCALATION}",
    )
