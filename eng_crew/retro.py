"""The retro step — what a shipped ticket leaves behind for the next one.

A ticket's history was write-only. Every run, every cost against budget, every
review verdict and every retry was recorded in ``ticket_events`` and then read
by nobody: the ticket shipped, and the next ticket started from exactly the same
blank slate as the last one. A team that never holds a retro repeats itself, and
so did this one.

This module closes that loop at the one point where a ticket's history is
finally complete — ``done -> shipped``:

  - **Write.** :func:`write_retro` reads the ticket's own record — the runs that
    attempted it, what it spent against its budget, the reviewer's verdicts, the
    QA pass, how many builds it took — and writes one short retro into the
    existing lessons store (``memories``, via :func:`eng_crew.tracker.add_memory`).
  - **Read.** :func:`lessons_block` selects the retros relevant to a new task and
    renders them for a builder's prompt, so the lessons are in front of the agent
    that could act on them rather than in a table.

Three properties keep it honest:

  - **It costs nothing.** The retro is assembled from recorded facts, not from a
    model call. A ticket that just shipped has already spent its budget on the
    work; a retro that billed for the privilege of summarising it would be the
    first paid step nobody authorised.
  - **It never blocks the ship.** A ticket ships because a person said so. If the
    retro cannot be written, that is logged and the ship stands.
  - **It is written once, where every surface goes through.** The hook is in
    :func:`eng_crew.tickets.move`, next to the budget check and for the same
    reason: the dashboard, the CLI and the MCP server all move tickets, and none
    of them should have to remember.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import tracker

log = logging.getLogger(__name__)

#: Who the ticket's history credits for the retro. Not a person — see
#: tickets.ROBOTS.
ACTOR = "retro"

#: Every note and every stored retro title starts here, so both the ticket's
#: history and the lessons store are greppable.
NOTE_PREFIX = "retro:"
TITLE_PREFIX = "Retro —"

#: How many retros a builder is shown, and how long each may be. A lesson that
#: crowds out the task is not a lesson.
MAX_LESSONS = 4
MAX_LESSON_CHARS = 600

#: How far back the store is searched for relevant retros.
LESSON_POOL = 40

#: Words too common to say anything about what a ticket was about.
_STOPWORDS = frozenset("""
a an and are as at be but by for from has have how if in into is it its of on or
that the their then there these this to was were what when where which who will
with add adds added make makes making use uses using do does doing new so we you
""".split())

_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")

#: The reviewer writes "approve" as a note and "approved" as the reason for the
#: in_qa move. Both are one verdict.
_VERDICT_ALIASES = {"approved": "approve"}


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as _default
    return _default


# ── Writing: a ticket's history, distilled ────────────────────────────────────

def history(ticket_id: int, settings: Any = None) -> dict | None:
    """The facts a retro is made of, read off the ticket's own record.

    Everything here is already recorded — runs, spend, ticket_events. Gathering
    it in one place is what makes the retro reproducible rather than a story an
    LLM told once.
    """
    from . import budget, tickets

    ticket = tracker.get_backlog_item(ticket_id)
    if not ticket:
        return None

    runs = tracker.get_ticket_runs(ticket_id)
    events = tickets.events(ticket_id)

    verdicts: list[tuple[str, str]] = []
    budget_stops: list[str] = []
    rejections = 0
    qa_passed = False
    for event in events:
        actor = (event.get("actor") or "").strip()
        note = (event.get("note") or "").strip()
        if actor == "reviewer" and note.startswith("review:"):
            body = note[len("review:"):].strip()
            verdict, _, reasons = body.partition("—")
            # The reviewer records its verdict twice when it approves: once as
            # a note, once as the reason for the in_qa move. Same verdict, one
            # entry — a retro that said "reviewed twice" would be wrong.
            word = verdict.strip()
            entry = (_VERDICT_ALIASES.get(word, word), reasons.strip())
            if entry not in verdicts:
                verdicts.append(entry)
        elif actor == "budget" and note.startswith("budget: stopped"):
            budget_stops.append(note)
        if event.get("to_status") == tickets.REJECTED:
            rejections += 1
        if event.get("actor") == "qa" and event.get("to_status") == tickets.DONE:
            qa_passed = True

    limit = budget.budget_for(ticket, settings)
    spent = tracker.ticket_spend(ticket_id)
    return {
        "ticket": ticket,
        "runs": runs,
        "builds": len(runs),
        "failed_runs": [r for r in runs if (r.get("status") or "") not in
                        ("completed", "unverified")],
        "unverified_runs": [r for r in runs if (r.get("status") or "") == "unverified"],
        "verdicts": verdicts,
        "changes_requested": [r for v, r in verdicts if v == "changes_requested"],
        "budget_stops": budget_stops,
        "rejections": rejections,
        "qa_passed": qa_passed,
        "budget_usd": limit,
        "spent_usd": spent,
        "files": _files_touched(ticket, runs),
    }


def _files_touched(ticket: dict, runs: list[dict]) -> list[str]:
    """The paths the ticket's last landed run changed. Best effort.

    The branch may be gone — swept, merged, renamed — and a retro is worth
    writing without it, so this never raises.
    """
    from . import git_skill

    project = ticket.get("project_path") or ""
    for run in reversed(runs):
        branch = run.get("git_branch") or ""
        if not project or not branch:
            continue
        try:
            return git_skill.branch_files(project, branch)
        except Exception as exc:
            log.debug("retro: no file list for branch %s: %s", branch, exc)
    return []


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one}" if n == 1 else f"{n} {many or one + 's'}"


def summarize(facts: dict) -> dict:
    """What went well, what did not, and the one line worth carrying forward.

    Written for the reader it is for: a builder starting the *next* ticket on
    this project, who needs the shape of what happened, not a transcript.
    """
    builds = facts["builds"]
    spent, limit = facts["spent_usd"], facts["budget_usd"]
    share = f"${spent:.2f} of a ${limit:.2f} budget"

    worked: list[str] = []
    if builds:
        worked.append(f"Shipped after {_plural(builds, 'build')}, spending {share}.")
    else:
        worked.append(f"Shipped with no recorded build ({share} spent).")
    if facts["qa_passed"]:
        worked.append("The verification gate passed on the run's branch.")
    if facts["files"]:
        worked.append("Landed in: " + ", ".join(facts["files"][:12]) +
                      ("…" if len(facts["files"]) > 12 else ""))

    didnt: list[str] = []
    if builds > 1:
        didnt.append(f"Took {builds} builds, not one.")
    if facts["rejections"]:
        didnt.append(f"Was rejected {_plural(facts['rejections'], 'time')} along the way.")
    for reasons in facts["changes_requested"][:2]:
        didnt.append(f"The reviewer requested changes: {reasons}")
    if facts["unverified_runs"]:
        didnt.append(
            f"{_plural(len(facts['unverified_runs']), 'run')} finished unverified — "
            "the tests and build produced no evidence."
        )
    if facts["budget_stops"]:
        didnt.append(
            f"Stopped on budget {_plural(len(facts['budget_stops']), 'time')}; "
            "someone had to raise it."
        )

    lesson_parts: list[str] = []
    if facts["changes_requested"]:
        lesson_parts.append(
            "The reviewer's objections here were: "
            + " / ".join(r for r in facts["changes_requested"][:2] if r)
        )
    if builds > 1 or facts["rejections"]:
        lesson_parts.append(
            "Work of this shape did not land first time — read the existing code "
            "in these files before changing them."
        )
    if facts["budget_stops"] or (limit and spent > limit * 0.75):
        lesson_parts.append(
            f"It ran close to its budget ({share}) — keep the change tight."
        )
    if not lesson_parts:
        lesson_parts.append(
            "Landed cleanly first time: a focused change in the files above, "
            "verified by the project's own tests."
        )

    return {
        "what_worked": " ".join(worked),
        "what_didnt": " ".join(didnt),
        "lesson": " ".join(lesson_parts),
    }


def write_retro(ticket_id: int, settings: Any = None) -> dict | None:
    """Write a shipped ticket's retro into the lessons store.

    Returns the retro (with its ``memory_id``), or None when there is nothing to
    write about. Records the retro in the ticket's history too, so the ship is
    the last event only if the retro failed.
    """
    from . import tickets

    facts = history(ticket_id, settings)
    if not facts:
        log.warning("retro: no ticket %s", ticket_id)
        return None

    ticket = facts["ticket"]
    parts = summarize(facts)
    title = f"{TITLE_PREFIX} {str(ticket.get('title') or '').strip() or f'ticket {ticket_id}'}"
    task = tickets.task_text(ticket)
    body = "\n".join([
        title,
        f"ticket: {ticket_id}",
        f"runs: {', '.join(str(r['id']) for r in facts['runs']) or 'none'}",
        f"spend: ${facts['spent_usd']:.2f} of ${facts['budget_usd']:.2f}",
        f"review: {facts['verdicts'][-1][0] if facts['verdicts'] else 'not reviewed'}",
        f"qa: {'passed' if facts['qa_passed'] else 'not recorded'}",
        "",
        parts["what_worked"],
        parts["what_didnt"],
        parts["lesson"],
    ])

    last_run = facts["runs"][-1]["id"] if facts["runs"] else None
    memory_id = tracker.add_memory(
        run_id=last_run,
        project_path=ticket.get("project_path") or "",
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        title=title,
        task=task,
        what_worked=parts["what_worked"],
        what_didnt=parts["what_didnt"],
        lesson=parts["lesson"],
        files_touched=", ".join(facts["files"]),
        raw_text=body,
    )
    tickets.note(
        ticket_id, ACTOR,
        f"{NOTE_PREFIX} written to the lessons store (memory {memory_id}) — {parts['lesson']}",
    )
    return {**parts, "memory_id": memory_id, "title": title, "raw_text": body}


def on_shipped(ticket_id: int, settings: Any = None) -> dict | None:
    """The lifecycle hook. Never raises — a ticket ships whatever happens here."""
    try:
        return write_retro(ticket_id, settings)
    except Exception:
        log.exception("retro for ticket %s failed; the ship stands", ticket_id)
        return None


# ── Reading: the retros a new task should start with ──────────────────────────

def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS}


def _score(memory: dict, wanted: set[str]) -> int:
    """How much a retro has to say about this task — shared significant words."""
    if not wanted:
        return 0
    haystack = " ".join(str(memory.get(k) or "") for k in
                        ("title", "task", "lesson", "files_touched"))
    return len(wanted & _keywords(haystack))


def is_retro(memory: dict) -> bool:
    return str(memory.get("title") or "").startswith(TITLE_PREFIX)


def lessons_for(project_path: str, task: str = "", limit: int = MAX_LESSONS) -> list[dict]:
    """The past retros worth showing a builder starting ``task``.

    Relevance is word overlap with the task, and recency breaks the tie — a
    retro that names the files you are about to touch beats an older one that
    does not. Retros from other projects are eligible but rank below this
    project's, because :func:`eng_crew.tracker.get_memories` already returns
    them that way and the order is preserved for equal scores.
    """
    pool = [m for m in tracker.get_memories(
        project_path or None, limit_project=LESSON_POOL, limit_global=LESSON_POOL // 4,
    ) if is_retro(m)]
    wanted = _keywords(task)
    ranked = sorted(
        enumerate(pool),
        key=lambda pair: (-_score(pair[1], wanted), pair[0]),
    )
    return [m for _, m in ranked[:max(0, limit)]]


def lessons_block(project_path: str, task: str = "", limit: int = MAX_LESSONS) -> str:
    """The retros rendered for a prompt, or "" when there are none.

    Deliberately short and advisory. These are notes from past tickets on this
    project, not instructions: a stale lesson must not outrank the code in front
    of the agent.
    """
    lessons = lessons_for(project_path, task, limit)
    if not lessons:
        return ""
    lines = [
        "=== LESSONS FROM PAST TICKETS ===",
        "Retros from tickets this project already shipped. Treat them as hints "
        "about this codebase, not as requirements — the code wins where they disagree.",
        "",
    ]
    for memory in lessons:
        title = str(memory.get("title") or "").strip() or "Retro"
        lesson = str(memory.get("lesson") or "").strip()
        didnt = str(memory.get("what_didnt") or "").strip()
        files = str(memory.get("files_touched") or "").strip()
        entry = f"- {title}: {lesson}"
        if didnt:
            entry += f" (what went wrong: {didnt})"
        if files:
            entry += f" [files: {files}]"
        lines.append(entry[:MAX_LESSON_CHARS])
    return "\n".join(lines)
