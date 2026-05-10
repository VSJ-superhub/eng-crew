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
) -> TeamState:
    """Run a single task end-to-end. Blocking."""
    cfg = settings or load_settings()
    return run_pipeline(task=task, project_path=project_path, settings=cfg)


def run_project(
    goal: str,
    project_id: int,
    project_path: str,
    claude_md_path: str = "",
    plan_id: Optional[int] = None,
    *,
    settings=None,
) -> TeamState:
    """Run a planning task for a project (same as run_task — plan mode is not yet separate)."""
    cfg = settings or load_settings()
    return run_pipeline(task=goal, project_path=project_path, settings=cfg)


def run_sprint(plan_sprint_id: int, *, settings=None) -> None:
    """Execute a planned sprint by its plan_sprint_id."""
    from . import tracker
    cfg = settings or load_settings()
    row = tracker.get_plan_sprint_tasks(plan_sprint_id)
    if not row:
        log.warning("run_sprint: no tasks found for plan_sprint_id=%s", plan_sprint_id)
        return
    # Get project_path from the plan
    sprint_info = tracker._connect().execute(
        "SELECT pp.project_id, p.project_path FROM plan_sprints ps "
        "JOIN project_plans pp ON ps.plan_id = pp.id "
        "JOIN projects p ON pp.project_id = p.id "
        "WHERE ps.id = ?", (plan_sprint_id,)
    ).fetchone()
    if not sprint_info:
        log.warning("run_sprint: could not find project for plan_sprint_id=%s", plan_sprint_id)
        return
    project_path = sprint_info["project_path"]
    task = f"Execute sprint {plan_sprint_id}"
    run_pipeline(task=task, project_path=project_path, settings=cfg)


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
