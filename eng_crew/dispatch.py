"""Dispatching a free-text task — the ticket route in, for callers with no UI.

`run_task` (the MCP tool, and anything else that hands eng-crew a sentence) used
to fork straight into the pipeline. That build got none of what a ticket's build
gets: no acceptance criteria, so nothing for the independent reviewer to check it
against; no ticket, so no budget cap and no retro; no WIP limit, so a chat window
could start any number of them at once.

So a dispatch is now a ticket, filed and built through the same path the queue
and the dashboard use:

  1. Acceptance criteria. Given by the caller, or the Manager drafts them from
     the task and the real codebase (:func:`eng_crew.manager.draft_ticket`). A
     dispatch with no definition of done files a ticket and starts nothing —
     the lifecycle would refuse to make it ready anyway.
  2. Approval. Dispatching *is* the human saying "build this", so this readies
     the ticket on the caller's behalf — the one step the Manager is not allowed
     to take for itself.
  3. Build. `eng-crew ticket <id>` in a subprocess, exactly what the dashboard
     launches, so review, budget and retro all apply.

The WIP limit is honoured rather than worked around: over the limit, the ticket
is filed and readied but no build starts, and the caller is told it is queued.
Nothing is lost and nothing retries it — pulling it is the queue's job.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Dispatch:
    """What a dispatch did. ``ok`` is False only when nothing was filed or built."""
    ok: bool
    detail: str
    ticket_id: int | None = None
    started: bool = False
    #: "given" when the caller supplied criteria, "manager" when it drafted them.
    criteria_source: str = ""
    wip: dict = field(default_factory=dict)
    log_path: str = ""
    warning: str = ""


def _python_exe() -> str:
    venv = _ROOT / ".venv" / "Scripts" / "python.exe"
    if not venv.exists():
        venv = _ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _title_of(task: str) -> str:
    """A ticket title from a free-text task: its first line, or first sentence."""
    first = next((line.strip() for line in str(task).splitlines() if line.strip()), "")
    if len(first) > 120:
        head, _, _ = first[:120].rpartition(" ")
        first = (head or first[:120]).rstrip(",;:") + "…"
    return first[:200]


def dispatch_task(
    project_path: str,
    task: str,
    acceptance_criteria: str | list[str] = "",
    *,
    base_branch: str = "",
    actor: str = "user",
    priority: int = 50,
    settings=None,
    env: dict | None = None,
    python_exe: str | None = None,
) -> Dispatch:
    """File a task as a ticket and build it. Returns without waiting for the build.

    ``actor`` is whoever dispatched — a person, since the dispatch is the
    approval that readies the ticket. Robots dispatch through the queue instead.
    """
    from . import git_skill, manager, tickets, tracker
    from .config import load_settings

    proj = Path(project_path).expanduser()
    if not proj.is_dir():
        return Dispatch(False, f"'{project_path}' is not a directory")
    proj = proj.resolve()
    path = str(proj)

    base_branch = (base_branch or "").strip()
    if base_branch and not git_skill.branch_exists(path, base_branch):
        # Refuse before filing anything: a ticket whose build cannot start is
        # worse than an error the caller can fix and repeat.
        return Dispatch(False, f"no branch '{base_branch}' in {path}")

    if not str(task or "").strip():
        return Dispatch(False, "a dispatch needs a task")

    cfg = settings or load_settings()
    project_id = tracker.ensure_project(path)

    criteria = manager.criteria_text(acceptance_criteria)
    source = "given"
    if not criteria:
        # No definition of done came with the task, so the product manager
        # writes one against the actual code before anything is built.
        try:
            proposal = manager.draft_ticket(task, path, _orientation(path))
        except manager.DraftFailed as exc:
            return Dispatch(
                False,
                f"{exc} — dispatch it again with acceptance_criteria of your own",
            )
        source = "manager"
    else:
        proposal = {
            "title": _title_of(task),
            "description": str(task).strip(),
            "acceptance_criteria": criteria,
        }

    filed = manager.accept(
        proposal,
        path,
        actor="manager" if source == "manager" else actor,
        claude_md_path=str(proj / "CLAUDE.md"),
        project_id=project_id,
        priority=priority,
    )
    ticket_id = filed["ticket_id"]
    wip = tickets.wip(path, cfg)

    # Dispatching is the approval. This is the human's move, not the manager's —
    # which is why it is credited to the caller and not to `manager`.
    try:
        tickets.move(ticket_id, tickets.READY, actor, "dispatched as a task", settings=cfg)
    except tickets.TransitionRefused as exc:
        return Dispatch(
            True, f"ticket {ticket_id} filed in backlog but not started: {exc}",
            ticket_id=ticket_id, criteria_source=source, wip=wip,
            warning=filed.get("warning", ""),
        )

    if wip["slots"] <= 0:
        return Dispatch(
            True,
            f"WIP limit reached ({wip['in_flight']}/{wip['limit']} in flight) — "
            f"ticket {ticket_id} is ready and waiting in the queue; nothing was started",
            ticket_id=ticket_id, criteria_source=source, wip=wip,
        )

    started = _claim_and_launch(
        ticket_id, path, actor,
        base_branch=base_branch, settings=cfg, env=env, python_exe=python_exe,
    )
    started.criteria_source = source
    if not started.started and started.ok:
        started.detail = (
            f"ticket {ticket_id} is ready but its build did not start: {started.detail}"
        )
    return started


def _claim_and_launch(
    ticket_id: int,
    project_path: str,
    actor: str,
    *,
    base_branch: str = "",
    settings=None,
    env: dict | None = None,
    python_exe: str | None = None,
) -> Dispatch:
    """Claim a `ready` ticket and run `eng-crew ticket <id>` on it.

    The claim happens here rather than inside the subprocess, so a refusal —
    the WIP limit, an exhausted budget — comes back to the caller with its
    reason instead of dying in a log nobody reads.
    """
    from . import tickets

    cfg = settings
    try:
        tickets.move(ticket_id, tickets.IN_PROGRESS, actor, "build requested", settings=cfg)
    except tickets.TransitionRefused as exc:
        return Dispatch(True, str(exc), ticket_id=ticket_id, wip=tickets.wip(project_path, cfg))

    from .config import DATA_DIR

    log_file = DATA_DIR / "logs" / f"run_{int(time.time())}.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            python_exe or _python_exe(), "-u", "-m", "eng_crew", "ticket", str(ticket_id),
            "--no-approval", "--log-path", str(log_file),
            *(["--base", base_branch] if base_branch else []),
        ]
        with open(log_file, "w") as log:
            subprocess.Popen(
                cmd,
                cwd=str(_ROOT),
                env={
                    **(env or os.environ),
                    "PYTHONIOENCODING": "utf-8",
                    "ENG_CREW_REQUIRE_APPROVAL": "false",
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
                ),
            )
    except Exception as exc:
        # The claim has to come back off, or the ticket holds a WIP slot for a
        # build that never ran.
        try:
            tickets.move(ticket_id, tickets.BACKLOG, actor, f"launch failed: {exc}")
        except tickets.TransitionRefused:
            pass
        return Dispatch(
            False, f"ticket {ticket_id} was filed but its build could not be launched: {exc}",
            ticket_id=ticket_id, wip=tickets.wip(project_path, cfg),
        )

    return Dispatch(
        True, f"building ticket {ticket_id}", ticket_id=ticket_id, started=True,
        wip=tickets.wip(project_path, cfg), log_path=str(log_file),
    )


def build_ticket(
    ticket_id: int,
    *,
    actor: str = "user",
    base_branch: str = "",
    settings=None,
    env: dict | None = None,
    python_exe: str | None = None,
) -> Dispatch:
    """Build a ticket that already exists. Creates nothing.

    The counterpart to :func:`dispatch_task` for a surface whose user is looking
    at a queue — Discord's ``/build``, say — rather than describing new work.
    Approving a ``backlog`` (or previously ``rejected``) ticket to ``ready`` is
    the human's move, so it is credited to ``actor``; everything after that is
    the same path the dashboard and the CLI take, under the same WIP limit and
    the same budget.
    """
    from . import basing, budget, tickets, tracker
    from .config import load_settings

    cfg = settings or load_settings()
    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        return Dispatch(False, f"no ticket {ticket_id}")

    path = ticket.get("project_path") or ""
    status = ticket.get("status") or tickets.BACKLOG
    if status in tickets.IN_FLIGHT or status in (tickets.DONE, tickets.SHIPPED):
        return Dispatch(
            False, f"ticket {ticket_id} is {status} — it is not waiting to be built",
            ticket_id=ticket_id,
        )

    # Prerequisites before anything else: a blocked ticket must stay exactly
    # where it is, so this is checked before the approval to `ready` as well as
    # before the claim (the lifecycle checks the claim itself).
    unmet = tickets.unmet_prerequisites(ticket)
    if unmet:
        return Dispatch(
            False,
            f"ticket {ticket_id} is blocked by "
            f"{tickets.describe_prerequisites(unmet)} — finish them first; "
            f"nothing was started",
            ticket_id=ticket_id,
        )

    # The prerequisites are done, but their branches still have to combine for
    # this build to start from them. Checked here, before the approval, for the
    # same reason: a ticket that cannot be built stays where it was.
    try:
        basing.ensure_buildable(ticket, explicit_base=base_branch)
    except basing.ConflictingPrerequisites as exc:
        return Dispatch(False, f"{exc}; nothing was started", ticket_id=ticket_id)

    if status in (tickets.BACKLOG, tickets.REJECTED):
        # Approving it is the step a person has to take; a ticket with no
        # definition of done is refused here, by the lifecycle, with its reason.
        try:
            tickets.move(ticket_id, tickets.READY, actor, "approved to build", settings=cfg)
        except tickets.TransitionRefused as exc:
            return Dispatch(False, str(exc), ticket_id=ticket_id)

    spend = budget.status(ticket_id, cfg)
    if spend and spend.get("exhausted"):
        return Dispatch(
            True,
            f"ticket {ticket_id} has spent ${spend['spent_usd']:.2f} of its "
            f"${spend['budget_usd']:.2f} budget — raise it "
            f"(`eng-crew budget {ticket_id} <amount>`) and build it again; "
            f"nothing was started",
            ticket_id=ticket_id, wip=tickets.wip(path, cfg),
        )

    wip = tickets.wip(path, cfg)
    if wip["slots"] <= 0:
        return Dispatch(
            True,
            f"WIP limit reached ({wip['in_flight']}/{wip['limit']} in flight) — "
            f"ticket {ticket_id} is ready and waiting in the queue; nothing was started",
            ticket_id=ticket_id, wip=wip,
        )

    # One build per project at a time: runs share the project's checkout.
    if tracker.get_active_runs_for_project(path):
        return Dispatch(
            True,
            f"a run is already active for this project — ticket {ticket_id} is "
            f"ready and waiting; nothing was started",
            ticket_id=ticket_id, wip=wip,
        )

    return _claim_and_launch(
        ticket_id, path, actor,
        base_branch=base_branch, settings=cfg, env=env, python_exe=python_exe,
    )


def _orientation(project_path: str, limit: int = 4000) -> str:
    """Rendered project docs for the manager's prompt. Never raises."""
    try:
        from .project_context import load_project_context

        return load_project_context(project_path).render()[:limit]
    except Exception:
        return ""
