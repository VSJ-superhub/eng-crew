"""High-level run helpers — wraps pipeline.run_pipeline for dashboard and CLI use."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .config import load_settings
from .pipeline import run_pipeline
from .state import TeamState

log = logging.getLogger(__name__)


def run_task(
    task: str,
    project_path: str,
    claude_md_path: str = "",
    *,
    settings=None,
    run_id: int | None = None,
) -> TeamState:
    """Run a single task end-to-end. Blocking.

    ``run_id`` lets a caller (e.g. the Discord bot) pre-create the run row so
    it can bridge the HITL approval gate via tracker.set_hitl_decision while
    the pipeline polls with tracker.get_hitl_decision.
    """
    cfg = settings or load_settings()
    return run_pipeline(task=task, project_path=project_path, settings=cfg, run_id=run_id)


def run_project(
    goal: str,
    project_id: int,
    project_path: str,
    claude_md_path: str = "",
    plan_id: Optional[int] = None,
    *,
    settings=None,
) -> list[dict]:
    """Plan a project: decompose the goal into tickets. Does not build anything.

    Returns the tickets created. Building is a separate step — pull a ticket off
    the queue with :func:`run_ticket`.
    """
    from .planning import plan_project

    cfg = settings or load_settings()
    return plan_project(
        goal=goal,
        project_id=project_id,
        project_path=project_path,
        claude_md_path=claude_md_path,
        plan_id=plan_id,
        settings=cfg,
    )


def run_ticket(ticket_id: int, *, actor: str = "queue", log_path: str | None = None,
               base_branch: str | None = None, settings=None) -> TeamState | None:
    """Build one ticket. Blocking.

    The task handed to the pipeline is the ticket itself — title, description,
    and acceptance criteria — and the run row is linked back to the ticket, so
    the ticket's history shows every run that attempted it. Moving the ticket to
    in_progress goes through the lifecycle, which means the WIP limit is
    enforced here too: over the limit, nothing is launched. A caller that
    already claimed the ticket (the dashboard does, so it can refuse over-limit
    requests with an HTTP error) finds it in_progress and keeps it there.

    ``base_branch`` stacks the ticket's run on that ref instead of the project
    checkout's HEAD, the same as a plain task run. Left out, the base is worked
    out from the ticket's prerequisites (:mod:`eng_crew.basing`): a prerequisite
    that is done but whose branch nobody has merged is still work this build
    must start from, or it would be built again. This is where every surface
    resolves it, because `eng-crew ticket <id>` is what all of them launch.
    """
    from . import basing, tickets, tracker

    cfg = settings or load_settings()
    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        log.warning("run_ticket: no ticket %s", ticket_id)
        return None

    # Unfinished prerequisites first: that refusal is the lifecycle's, and
    # there is nothing to base on until they are done. Then the base itself —
    # before the claim, so a ticket whose prerequisites cannot be combined
    # stays exactly where it is, with the conflict named.
    unmet = tickets.unmet_prerequisites(ticket)
    if unmet:
        raise tickets.Blocked(ticket_id, unmet)
    base = basing.resolve_base(ticket, explicit_base=base_branch or "")

    if ticket["status"] != tickets.IN_PROGRESS:
        tickets.move(ticket_id, tickets.IN_PROGRESS, actor, "build started", settings=cfg)

    task = tickets.task_text(ticket)
    project_path = ticket["project_path"]
    run_id = tracker.create_run(task, project_path, log_path=log_path, ticket_id=ticket_id)
    tracker.update_backlog_item(ticket_id, run_id=run_id)
    tracker.update_run_base(run_id, base.ref or "HEAD", base.reason)
    log.info("ticket %s builds on %s (%s)", ticket_id, base.ref or "HEAD", base.reason)

    try:
        state = run_pipeline(
            task=task, project_path=project_path, settings=cfg, run_id=run_id,
            base_branch=base.ref,
        )
    except Exception:
        _settle_ticket(ticket_id, tickets.REJECTED, actor, f"run {run_id} raised")
        raise

    status = tracker.get_run_detail(run_id)
    landed = bool(status) and status["status"] in ("completed", "unverified")
    _settle_ticket(
        ticket_id,
        tickets.IN_REVIEW if landed else tickets.REJECTED,
        actor,
        f"run {run_id} finished as {status['status'] if status else 'unknown'}",
    )
    if landed:
        _review_ticket(ticket_id, run_id, state, cfg)
    return state


def _review_ticket(ticket_id: int, run_id: int, state, settings) -> None:
    """Hand the landed run to the independent reviewer. Never raises.

    The build already happened; a reviewer that cannot be reached or is not
    configured must leave the ticket awaiting review rather than lose the run.
    """
    from . import review

    try:
        # The diff is read from the run's branch, measured against the base
        # the run was built on (runs.base_branch) — not from the state.
        verdict = review.review_ticket(ticket_id, run_id, settings=settings)
    except review.ReviewUnavailable as exc:
        log.warning("ticket %s was not reviewed: %s", ticket_id, exc)
    except Exception:
        log.exception("reviewing ticket %s failed", ticket_id)
    else:
        if verdict:
            log.info(
                "ticket %s review: %s -> %s", ticket_id, verdict["verdict"], verdict["status"]
            )


def _settle_ticket(ticket_id: int, to_status: str, actor: str, note: str) -> None:
    """Move a ticket off in_progress once its run is over. Never raises — the
    run already happened, and a refused transition must not mask its result."""
    from . import tickets

    try:
        tickets.move(ticket_id, to_status, actor, note)
    except tickets.TransitionRefused as exc:
        log.warning("ticket %s stayed put after its run: %s", ticket_id, exc)


def resume_run(run_id: int, clarification_answer: str = "") -> bool:
    """Resume a previously interrupted run. Returns True if resumed, False if state not found."""
    from . import tracker
    run = tracker.get_run_detail(run_id)
    if not run:
        return False
    cfg = load_settings()
    try:
        run_pipeline(task=run["task_text"], project_path=run["project_path"], settings=cfg)
        return True
    except Exception as exc:
        log.error("resume_run(%s) failed: %s", run_id, exc)
        return False
