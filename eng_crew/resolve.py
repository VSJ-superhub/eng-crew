"""Resolving the conflicts that stop a ticket being built on its prerequisites.

A dependent ticket's run forks from its prerequisites' work (eng_crew/basing.py).
When that work does not merge onto HEAD — HEAD moved on, or two prerequisites
touched the same lines — the build is refused before it starts. Saying "merge it
yourself" left a dashboard user with nothing to click: the only way forward was
a git checkout and a text editor.

:func:`resolve` does that merge instead, on a branch of its own
(:func:`eng_crew.basing.resolved_branch`) in a worktree, so the checkout the
developer is sitting in never changes branch and never sees a conflict. Each
prerequisite branch is merged onto HEAD in turn; one that conflicts is handed
to an agent — the builder's role, with the builder's tools — to resolve, and
the result is only committed once no conflict is left in it. The project's
own checks then run on the merged tree. Only a resolution that passes them is
kept: a broken base is worse than no build, since every run built on it would
inherit the breakage.

Its cost is the ticket's, like every paid step: the budget is checked before
the agent starts, the session is capped at what the ticket has left, and the
spend is recorded on a run linked to the ticket. That run carries no branch of
its own — its ``base_branch`` is the resolution — so nothing mistakes it for
the ticket's work. The outcome is a note in the ticket's history.

A resolution starts no build. Building is still the person's call, and the
next build finds the resolved branch and forks from it.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import basing, budget, git_skill, prompts, tickets, tracker
from . import verify as verify_mod

log = logging.getLogger(__name__)

#: Who the ticket's history credits with a resolution.
ACTOR = "resolver"

#: Every note this module writes starts here.
NOTE_PREFIX = "resolve:"

#: The run a resolution is charged to is titled with this.
TASK_PREFIX = "Resolve merge conflicts"

#: The step name a budget stop records.
STEP = "conflict resolution"

TOOLS = "Glob,Grep,Read,Edit,Write,Bash"
MAX_TURNS = 40

#: A resolution is for a ticket waiting to be built. Once a build has started
#: its base is fixed, and changing the branch under it would change nothing.
RESOLVABLE = frozenset({tickets.BACKLOG, tickets.READY})

_MARKERS = ("<<<<<<< ", ">>>>>>> ")


class ResolveRefused(Exception):
    """There is nothing this ticket's state lets a resolution do."""


class ResolveFailed(Exception):
    """The resolution was attempted and did not produce a usable base."""


@dataclass
class Resolution:
    """What a resolution produced. ``branch`` is None when nothing needed it."""
    branch: str | None
    summary: str
    cost_usd: float = 0.0
    run_id: int | None = None


#: Tickets with a resolution running in this process.
_resolving: set[int] = set()
_resolving_lock = threading.Lock()


def resolving(ticket_id: int) -> bool:
    """Whether a resolution of this ticket is running in this process."""
    with _resolving_lock:
        return ticket_id in _resolving


def resolve(ticket_id: int, settings: Any = None) -> Resolution:
    """Merge a ticket's unmerged prerequisites onto HEAD, resolving conflicts.

    Raises :class:`ResolveRefused` when the ticket is not waiting to be built
    or a resolution of it is already running, :class:`budget.BudgetExhausted`
    when it has no budget left, and :class:`ResolveFailed` when the agent
    could not produce a merge that passes the project's checks — recorded in
    the ticket's history either way.
    """
    with _resolving_lock:
        if ticket_id in _resolving:
            raise ResolveRefused(f"conflicts on #{ticket_id} are already being resolved")
        _resolving.add(ticket_id)
    try:
        return _resolve(ticket_id, settings)
    finally:
        with _resolving_lock:
            _resolving.discard(ticket_id)


def _settings(settings: Any) -> Any:
    if settings is not None:
        return settings
    from .config import Settings

    return Settings()


def _resolve(ticket_id: int, settings: Any) -> Resolution:
    settings = _settings(settings)
    row = tracker.get_backlog_item(ticket_id)
    if not row:
        raise ResolveRefused(f"ticket {ticket_id} not found")
    if row.get("status") not in RESOLVABLE:
        raise ResolveRefused(
            f"#{ticket_id} is {row.get('status')}; conflicts are resolved before "
            f"a ticket is built")
    project_path = row.get("project_path") or ""

    try:
        basing.ensure_buildable(row)
    except basing.ConflictingPrerequisites:
        pass
    else:
        return Resolution(None, "nothing to resolve — the build can start")

    unmerged, refs = basing.unmerged_prerequisites(row)
    state = budget.check(ticket_id, STEP, settings)
    remaining = state["remaining_usd"] if state else None

    branch = basing.resolved_branch(ticket_id)
    _clear(project_path, branch)
    named = ", ".join(f"#{i}" for i, _ in unmerged)
    run_id = tracker.create_run(
        f"{TASK_PREFIX} for #{ticket_id}: merging {named} onto HEAD",
        project_path, ticket_id=ticket_id,
    )
    tracker.update_run_base(run_id, branch, f"merging {named} onto HEAD")

    wt: Path | None = None
    cost = 0.0
    accounts: list[str] = []
    try:
        wt = git_skill.create_worktree(
            project_path, branch, worktree_dir=settings.worktree_dir or None, base="HEAD",
        )
        names = [n.strip() for n in settings.worktree_link_dirs.split(",") if n.strip()]
        git_skill.link_into_worktree(git_skill.repo_root(project_path), wt, names)
        for ref in refs:
            merged = subprocess.run(
                ["git", "merge", "--no-edit", ref],
                cwd=wt, capture_output=True, encoding="utf-8", errors="replace",
            )
            if merged.returncode == 0:
                continue
            conflicted = _conflicted(wt)
            if not conflicted:
                raise ResolveFailed(f"merging {ref} failed: {(merged.stderr or merged.stdout).strip()}")
            spent, account = _ask_agent(
                row, ref, conflicted, wt, run_id, settings,
                None if remaining is None else max(0.0, remaining - cost),
            )
            cost += spent
            left = _conflicted(wt) + _with_markers(wt, conflicted)
            if left:
                raise ResolveFailed(f"the agent left conflicts in {', '.join(sorted(set(left)))}")
            git_skill.commit_all(wt, f"Merge {ref} onto HEAD for #{ticket_id}, resolving "
                                     f"{', '.join(conflicted)}")
            accounts.append(account)

        result = verify_mod.verify(wt)
        if not result.passed:
            raise ResolveFailed(f"the merged tree fails the project's checks: {result.summary()}")
    except Exception as exc:
        if wt is not None:
            subprocess.run(["git", "merge", "--abort"], cwd=wt, capture_output=True)
            _remove(project_path, wt)
        try:
            git_skill.delete_branch(project_path, branch)
        except Exception:
            pass
        reason = str(exc) or type(exc).__name__
        tracker.finish_run(run_id, status="failed", final_summary=reason)
        tickets.note(ticket_id, ACTOR, f"{NOTE_PREFIX} could not resolve the conflicts "
                                       f"merging {named} onto HEAD — {reason}")
        if isinstance(exc, ResolveFailed):
            raise
        raise ResolveFailed(reason) from exc

    _remove(project_path, wt)
    summary = " ".join(a for a in accounts if a) or "merged"
    tracker.finish_run(run_id, status="completed", final_summary=summary)
    tickets.note(ticket_id, ACTOR, f"{NOTE_PREFIX} resolved the conflicts merging {named} "
                                   f"onto HEAD on {branch} — {summary[:600]}")
    return Resolution(branch, summary, cost, run_id)


def latest_note(ticket_id: int) -> str | None:
    """The last resolution's outcome, while the ticket has not been built since."""
    last = None
    for event in tickets.events(ticket_id):
        if event.get("to_status") == tickets.IN_PROGRESS and event.get("from_status") != tickets.IN_PROGRESS:
            last = None
        elif event.get("actor") == ACTOR and (event.get("note") or "").startswith(NOTE_PREFIX):
            last = event["note"]
    return last


def _ask_agent(row: dict, ref: str, conflicted: list[str], wt: Path, run_id: int,
               settings: Any, cap: float | None) -> tuple[float, str]:
    """Hand a stopped merge to the agent. Returns (cost, its account of it)."""
    from .providers import call_llm
    from .tracing import run_event_handler

    ticket_id = int(row["id"])
    prereqs = []
    for prerequisite_id in tickets.dependencies(row):
        p = tracker.get_backlog_item(prerequisite_id) or {}
        prereqs.append(f"- #{prerequisite_id} {p.get('title') or ''}\n"
                       f"{_indent(p.get('acceptance_criteria') or '')}")
    prompt = prompts.render(
        "resolve-merge-conflicts",
        ref=ref, ticket_id=ticket_id, title=row.get("title") or "",
        prerequisites="\n".join(prereqs) or "(none recorded)",
        conflicted="\n".join(f"- {c}" for c in conflicted),
    )
    cfg = settings.get_agent_config("single_agent")
    llm_result = call_llm(
        cfg["provider"], cfg["model"], prompt,
        allowed_tools=TOOLS, max_turns=MAX_TURNS, cwd=str(wt),
        on_event=run_event_handler(run_id, "resolve"),
        max_budget_usd=cap,
    )
    tracker.log_event(run_id, -1, "resolve_conflicts", llm_result)
    return float(llm_result.cost_usd or 0.0), (llm_result.text or "").strip()


def _indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.strip().splitlines())


def _conflicted(wt: Path) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=wt, capture_output=True, encoding="utf-8", errors="replace",
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _with_markers(wt: Path, paths: list[str]) -> list[str]:
    """Which of these files still hold a conflict marker at the start of a line."""
    left = []
    for rel in paths:
        try:
            text = (wt / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(line.startswith(_MARKERS) for line in text.splitlines()):
            left.append(rel)
    return left


def _clear(project_path: str, branch: str) -> None:
    """Drop an earlier resolution: it was made against a HEAD that has moved."""
    for entry in git_skill.list_worktrees(project_path):
        if entry.get("branch") == branch:
            _remove(project_path, Path(entry["path"]))
    if git_skill.branch_exists(project_path, branch):
        git_skill.delete_branch(project_path, branch)


def _remove(project_path: str, wt: Path) -> None:
    try:
        git_skill.remove_worktree(project_path, wt, force=True)
    except Exception as exc:
        log.warning("could not remove the resolution worktree %s: %s", wt, exc)
