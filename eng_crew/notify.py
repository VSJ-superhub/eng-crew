"""Reaching the person when a ticket needs them — a Discord webhook push.

A ticket's whole point is that nobody has to watch it. The gap that left was at
the other end: when a ticket *did* need a person — it finished, the reviewer
asked for changes, no reviewer could be reached, the run failed, somebody
rejected it, it spent its budget, or its prerequisites' branches would not
combine — it waited silently in the tracker until
whoever dispatched it thought to look. This module closes that gap with the
cheapest push that reaches a phone: a POST to a Discord channel webhook,
optionally @mentioning one configured user so the mobile notification fires.

Four properties make it safe to wire into the lifecycle:

  - **It fires from the lifecycle, not from a surface.** The hooks are in
    :func:`eng_crew.tickets.move` and :func:`eng_crew.tickets.note`, beside the
    budget check and the retro and for the same reason: the CLI, the dashboard,
    MCP ``run_task``, Discord and a queue pull all go through them, so all five
    notify identically — and none of it needs the Discord bot to be running.
  - **It is best-effort.** A network error, a timeout or a non-2xx reply is
    logged and dropped. A build must not fail, and a status change must not be
    refused, because a chat service was unreachable.
  - **It pings only who it was told to.** ``allowed_mentions`` names the single
    configured user and parses nothing else, and the ticket's own text has its
    mention syntax defanged, so an ``@everyone`` in a title cannot wake a
    server even on a client that ignores ``allowed_mentions``.
  - **The URL is a secret.** A webhook URL is a bearer credential: anyone
    holding it can post to the channel. It is never written to a log, to
    ``ticket_events`` or to the dashboard — :func:`redact` scrubs it out of
    anything this module reports, including an exception message that
    helpfully quoted it back.

With no webhook configured, nothing here does anything at all.
"""
from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any

import httpx

from . import tracker

log = logging.getLogger(__name__)

#: How long a push may take. A ticket's status change waits on this, so it is
#: short on purpose: a chat service having a bad day costs seconds, not builds.
TIMEOUT_SECONDS = 4.0

#: The things worth a push — every one of them is a ticket waiting on a person.
DONE = "done"
CHANGES_REQUESTED = "changes_requested"
REVIEW_UNAVAILABLE = "review_unavailable"
RUN_FAILED = "run_failed"
REJECTED = "rejected"
BUDGET_STOPPED = "budget_stopped"
CONFLICT = "conflict"
RESOLVED = "resolved"
RESOLVE_FAILED = "resolve_failed"

#: How each reads in the message's first line.
HEADLINES: dict[str, str] = {
    DONE: "is done",
    CHANGES_REQUESTED: "has changes requested",
    REVIEW_UNAVAILABLE: "has a review unavailable",
    RUN_FAILED: "had its run fail",
    REJECTED: "was rejected",
    BUDGET_STOPPED: "was stopped by its budget",
    CONFLICT: "cannot be built — its prerequisites conflict",
    RESOLVED: "has its conflicts resolved",
    RESOLVE_FAILED: "still has its conflicts",
}

#: The last line of the message for the kinds where there is something to do
#: about it from the same chat the message arrived in. ``{id}`` is the ticket's.
#: A conflict is the one refusal a phone can actually clear, so it says how.
TRAILERS: dict[str, str] = {
    CONFLICT: "Resolve it here: `/resolve {id}`",
    RESOLVED: "The next build forks from it — `/build {id}`.",
    RESOLVE_FAILED: "Try again with `/resolve {id}`, or merge it by hand.",
}

#: The bracketed line the verification gate leaves in a run's final summary.
#: For a failed run that one line is the reason, and it beats "finished as
#: failed" by exactly the amount a person needs to decide what to do next.
_GATE_MARKERS = (
    "[VERIFICATION FAILED]", "[RED]", "[TEST LOCK]", "[UNTESTED]",
    "[NOT VERIFIED]", "[UNVERIFIED]",
)

#: What a run-settling note looks like (eng_crew.run._settle_ticket), so a
#: rejection that is really a failed run is reported as one.
_RUN_NOTE = re.compile(r"\brun (\d+) (?:finished as (\w+)|raised)")


#: A mention, as Discord writes one: a user, a nickname, or a role.
_MENTION = re.compile(r"<@[!&]?(\d+)>")

#: Inserted after the @ so the text still reads as what it was without being a
#: mention any client would resolve.
_ZWSP = "​"

#: One line of reason is a notification; a reviewer's full argument is a wall.
MAX_REASON_CHARS = 400

#: Transitions already pushed, so one transition is one push even if the
#: surface asks twice (a double-clicked Run review, a retried settle).
_POSTED: OrderedDict[tuple, bool] = OrderedDict()
_POSTED_MAX = 512


def forget_posted() -> None:
    """Drop the de-duplication memory. For tests, and for a long-lived process
    that has no reason to remember last week's tickets."""
    _POSTED.clear()


def _settings(settings: Any = None) -> Any:
    if settings is not None:
        return settings
    from .config import settings as _default
    return _default


def webhook_url(settings: Any = None) -> str:
    return str(getattr(_settings(settings), "discord_webhook_url", "") or "").strip()


def notify_user_id(settings: Any = None) -> str:
    return str(getattr(_settings(settings), "discord_notify_user_id", "") or "").strip()


def enabled(settings: Any = None) -> bool:
    """True when there is somewhere to post. Everything here is a no-op if not."""
    return bool(webhook_url(settings))


def redact(text: Any, settings: Any = None) -> str:
    """``text`` with the webhook URL taken out of it.

    Everything this module logs goes through here. httpx is helpful enough to
    quote the URL it could not reach into its exception message, which is
    exactly how a bearer credential ends up in a log file.
    """
    out = str(text)
    url = webhook_url(settings)
    if url:
        out = out.replace(url, "<discord webhook>")
        # Also the token on its own: a redirect or a proxy may report only the
        # tail of the path, which is the half of the URL that is the secret.
        token = url.rsplit("/", 1)[-1]
        if len(token) > 8:
            out = out.replace(token, "<redacted>")
    return out


def defang(text: Any) -> str:
    """``text`` with its mention syntax made inert.

    ``allowed_mentions`` is the real guard; this is the one behind it, for a
    client that ignores it and for the plain unpleasantness of a ticket title
    rendering as a highlighted ping.
    """
    out = _MENTION.sub(lambda m: f"@{_ZWSP}{m.group(1)}", str(text))
    for word in ("everyone", "here"):
        out = out.replace(f"@{word}", f"@{_ZWSP}{word}")
    return out


def dashboard_base(settings: Any = None) -> str:
    """Where the dashboard is, for a link somebody will tap on a phone."""
    cfg = _settings(settings)
    explicit = str(getattr(cfg, "dashboard_url", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    host = str(getattr(cfg, "dashboard_host", "") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "*", ""):
        # Bound to every interface; "0.0.0.0" is not an address you can tap.
        host = "127.0.0.1"
    return f"http://{host}:{int(getattr(cfg, 'dashboard_port', 9000))}"


def ticket_link(ticket: dict, settings: Any = None) -> str:
    """The dashboard URL for one ticket — its project page, which is where its
    status moves, budget, review, area and history are."""
    base = dashboard_base(settings)
    project_id = ticket.get("project_id")
    if project_id:
        return f"{base}/projects/{project_id}/tasks?ticket={ticket.get('id')}"
    return f"{base}/backlog"


def compose(ticket: dict, kind: str, reason: str = "",
            settings: Any = None) -> dict:
    """The webhook payload: what happened, what it cost, why, and where to look."""
    cfg = _settings(settings)
    user_id = notify_user_id(cfg)
    spend = _spend_line(ticket, cfg)

    lines = []
    if user_id:
        lines.append(f"<@{user_id}>")
    lines.append(
        f"**#{ticket.get('id')} {HEADLINES.get(kind, kind)}** — "
        f"{defang(str(ticket.get('title') or 'untitled'))}"
    )
    detail = f"`{defang(str(ticket.get('project_path') or '?'))}`"
    if spend:
        detail += f" · {spend}"
    lines.append(detail)
    if reason:
        lines.append(defang(_one_line(reason)))
    trailer = TRAILERS.get(kind)
    if trailer:
        lines.append(trailer.format(id=ticket.get("id")))
    lines.append(f"<{ticket_link(ticket, cfg)}>")

    return {
        "content": "\n".join(lines),
        "allowed_mentions": {"parse": [], "users": [user_id] if user_id else []},
    }


def post(payload: dict, settings: Any = None) -> bool:
    """POST the payload. Never raises; True only when Discord accepted it."""
    url = webhook_url(settings)
    if not url:
        return False
    try:
        reply = httpx.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning(
            "discord notification not delivered: %s", redact(exc, settings)
        )
        return False
    status = getattr(reply, "status_code", 0)
    if not 200 <= int(status) < 300:
        log.warning(
            "discord notification refused with HTTP %s: %s",
            status, redact(getattr(reply, "text", ""), settings)[:200],
        )
        return False
    return True


def ticket_event(ticket_id: int, kind: str, reason: str = "",
                 *, run_id: int | None = None, settings: Any = None) -> bool:
    """Push one ticket event. The single entry point; never raises.

    Called from the lifecycle, where a raised exception would refuse a status
    change or fail a build — so nothing in here is allowed to escape.
    """
    try:
        cfg = _settings(settings)
        if not enabled(cfg) or kind not in HEADLINES:
            return False
        ticket = tracker.get_backlog_item(ticket_id)
        if not ticket:
            return False
        key = (int(ticket_id), kind, _one_line(reason), run_id or _run_id(ticket_id))
        if key in _POSTED:
            return False
        _POSTED[key] = True
        while len(_POSTED) > _POSTED_MAX:
            _POSTED.popitem(last=False)
        return post(compose(ticket, kind, reason, cfg), cfg)
    except Exception as exc:                                  # pragma: no cover
        log.warning("discord notification failed: %s", redact(exc, settings))
        return False


# -- the lifecycle hooks -------------------------------------------------------
#
# These two are what eng_crew.tickets calls. Everything above is reusable; the
# classification of "which recorded fact is worth a phone buzzing" lives here.


def on_transition(ticket_id: int, from_status: str, to_status: str,
                  note: str = "", *, settings: Any = None) -> bool:
    """A status change just landed. Push it if it is one a person waits on."""
    from . import tickets

    if to_status == tickets.DONE:
        return ticket_event(ticket_id, DONE, note, settings=settings)
    if to_status == tickets.REJECTED:
        match = _RUN_NOTE.search(note or "")
        if match:
            run_id = int(match.group(1))
            return ticket_event(
                ticket_id, RUN_FAILED, _gate_reason(run_id) or note,
                run_id=run_id, settings=settings,
            )
        return ticket_event(ticket_id, REJECTED, note, settings=settings)
    return False


def on_note(ticket_id: int, actor: str, text: str,
            *, settings: Any = None) -> bool:
    """Something was recorded about a ticket without moving it.

    Two of those are a person's problem — a changes-requested verdict and a
    review that could not run — and so is a budget stop, which is recorded the
    same way. So is a build refused because its prerequisites' branches do not
    combine, and the outcome of resolving that, either way. The rest (an
    approval, an area change, a budget raise, a dependency edit) are not, and
    are left alone.

    A conflict and a resolution are matched on the note itself rather than on
    who wrote it: a resolution is credited to whoever asked for it, which may
    be a person's Discord name.
    """
    from . import basing as basing_mod
    from . import budget as budget_mod
    from . import resolve as resolve_mod
    from . import review as review_mod

    note = (text or "").strip()
    if note.startswith(basing_mod.CONFLICT_NOTE_PREFIX):
        return ticket_event(
            ticket_id, CONFLICT,
            note[len(basing_mod.CONFLICT_NOTE_PREFIX):].strip(), settings=settings,
        )
    if note.startswith(resolve_mod.NOTE_PREFIX):
        outcome = note[len(resolve_mod.NOTE_PREFIX):].strip()
        kind = RESOLVE_FAILED if outcome.startswith(resolve_mod.FAILED_OUTCOME) else RESOLVED
        return ticket_event(ticket_id, kind, outcome, settings=settings)
    if actor == review_mod.REVIEWER_ACTOR:
        changes = f"{review_mod.NOTE_PREFIX} {review_mod.CHANGES_REQUESTED}"
        if note.startswith(changes):
            return ticket_event(
                ticket_id, CHANGES_REQUESTED,
                note[len(changes):].lstrip(" —-:"), settings=settings,
            )
        if note.startswith(review_mod.UNAVAILABLE_PREFIX):
            return ticket_event(
                ticket_id, REVIEW_UNAVAILABLE,
                note[len(review_mod.UNAVAILABLE_PREFIX):].strip(), settings=settings,
            )
    if actor == budget_mod.ACTOR and note.startswith(budget_mod.STOP_NOTE_PREFIX):
        return ticket_event(
            ticket_id, BUDGET_STOPPED,
            note[len(f"{budget_mod.NOTE_PREFIX}"):].strip(), settings=settings,
        )
    return False


# -- details -------------------------------------------------------------------

def _one_line(text: Any) -> str:
    """A reason as one line: a notification, not a transcript."""
    flat = " ".join(str(text or "").split())
    return flat[:MAX_REASON_CHARS - 1] + "…" if len(flat) > MAX_REASON_CHARS else flat


def _spend_line(ticket: dict, settings: Any = None) -> str:
    from . import budget as budget_mod

    try:
        limit = budget_mod.budget_for(ticket, settings)
        spent = tracker.ticket_spend(ticket["id"])
    except Exception:
        return ""
    return f"${spent:.2f} of ${limit:.2f}"


def _run_id(ticket_id: int) -> int:
    run = tracker.get_ticket_run(ticket_id) or {}
    return int(run.get("id") or 0)


def _gate_reason(run_id: int) -> str:
    """The verification gate's own line about a run, out of its final summary."""
    run = tracker.get_run_detail(run_id) or {}
    for line in str(run.get("final_summary") or "").splitlines():
        line = line.strip()
        if any(line.startswith(marker) for marker in _GATE_MARKERS):
            return line
    return ""
