"""Planning a project produces tickets.

This used to launch a build of the goal text — asking "Plan project: ship v2" got
you an agent trying to *implement* the string "ship v2". Planning is not a build:
it reads the project, decomposes the goal into tickets with acceptance criteria,
and puts them in the queue. Building happens later, one ticket at a time, when
someone pulls from that queue.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from . import prompts, tickets, tracker
from .agents.base import BaseAgent
from .project_context import load_project_context

_MAX_CONTEXT_CHARS = 6000
_MAX_TICKETS = 40


class PlanningFailed(RuntimeError):
    """The planner did not return a usable set of tickets."""


def _extract_json(text: str) -> dict:
    match = re.search(r"\{[\s\S]+\}", text or "")
    if not match:
        raise PlanningFailed("the planner returned no JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise PlanningFailed(f"the planner returned malformed JSON: {exc}") from exc


def _clean(raw: Any, index: int) -> dict | None:
    """Normalise one planner ticket. Returns None if it is not usable."""
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    criteria = raw.get("acceptance_criteria") or ""
    if isinstance(criteria, list):
        criteria = "\n".join(f"- {str(c).strip()}" for c in criteria if str(c).strip())
    criteria = str(criteria).strip()
    ticket_type = str(raw.get("type") or "feature").strip().lower()
    if ticket_type not in ("feature", "bug", "chore"):
        ticket_type = "feature"
    try:
        priority = int(raw.get("priority"))
    except (TypeError, ValueError):
        priority = (index + 1) * 10
    return {
        "title": title[:200],
        "description": str(raw.get("description") or title).strip(),
        "acceptance_criteria": criteria,
        "type": ticket_type,
        "priority": priority,
    }


def plan_tickets(
    goal: str,
    project_path: str,
    project_id: int | None = None,
    plan_id: int | None = None,
    *,
    settings: Any = None,
    actor: str = "planner",
    claude_md_path: str = "",
) -> list[dict]:
    """Decompose a goal into tickets and put them in the project's queue.

    Each ticket that came back with acceptance criteria is moved straight to
    `ready` — it is pullable. One that did not stays in `backlog` for a human to
    fill in, because the lifecycle will not let it through without them.
    """
    context = load_project_context(project_path).render()[:_MAX_CONTEXT_CHARS]
    prompt = prompts.render("plan-tickets", project_context=context, goal=goal)

    result = BaseAgent(settings).call(prompt, role="architect")
    data = _extract_json(result.text)
    raw_tickets = data.get("tickets")
    if not isinstance(raw_tickets, list) or not raw_tickets:
        raise PlanningFailed("the planner produced no tickets")

    created: list[dict] = []
    for index, raw in enumerate(raw_tickets[:_MAX_TICKETS]):
        cleaned = _clean(raw, index)
        if cleaned is None:
            continue
        ticket_id = tracker.add_backlog_item(
            title=cleaned["title"],
            description=cleaned["description"],
            project_path=project_path,
            claude_md_path=claude_md_path,
            priority=cleaned["priority"],
            project_id=project_id,
            item_type=cleaned["type"],
            acceptance_criteria=cleaned["acceptance_criteria"],
            created_by=actor,
            plan_id=plan_id,
        )
        if cleaned["acceptance_criteria"]:
            try:
                tickets.move(ticket_id, tickets.READY, actor,
                             "planned with acceptance criteria", settings=settings)
            except tickets.TransitionRefused as exc:
                print(f"[planning] ticket {ticket_id} stays in backlog: {exc}", file=sys.stderr)
        created.append(tracker.get_backlog_item(ticket_id) or {})

    if not created:
        raise PlanningFailed("the planner produced no usable tickets")
    return created


def plan_project(
    goal: str,
    project_id: int,
    project_path: str,
    claude_md_path: str = "",
    plan_id: int | None = None,
    *,
    settings: Any = None,
    actor: str = "planner",
) -> list[dict]:
    """plan_tickets, wrapped so a plan row records how the planning went.

    Safe to call on a background thread: a failure is recorded on the plan (which
    the dashboard surfaces) rather than vanishing into a dead thread.
    """
    if plan_id is None:
        plan_id = tracker.create_project_plan(project_id, goal)
    try:
        created = plan_tickets(
            goal, project_path, project_id, plan_id,
            settings=settings, actor=actor, claude_md_path=claude_md_path,
        )
    except Exception as exc:
        tracker.update_project_plan(plan_id, review_result=f"planning failed: {exc}")
        raise
    tracker.update_project_plan(
        plan_id, review_result=f"planned {len(created)} ticket(s)"
    )
    return created
