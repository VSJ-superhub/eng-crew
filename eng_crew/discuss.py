"""Discussion agent — a thinking partner, not a builder.

eng-crew's other agents all exist to produce a change. This one exists to talk:
to think through an idea before it is a task, to explain how something works, to
answer a question about your own code. It never writes anything.

Surface-agnostic, like `manager`: Discord is the first frontend, and the
dashboard or a CLI can call the same function. History is passed in per turn, so
the caller owns the conversation.

Distinct from `manager.chat()`, which is goal-directed — it pushes toward a
concrete buildable unit and emits a proposal. This one has no destination. Ask it
what a WAL journal is and it will just tell you.
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

from .providers import call_llm

DISCUSS_PROVIDER = "claude_cli"
DISCUSS_MODEL = "claude-sonnet-5"

# Read and search only. This agent must never be able to change anything —
# building is a separate, explicit step, exactly as with the manager.
DISCUSS_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch"
DISCUSS_MAX_TURNS = 12

# The tracker is an aside, not the message: a project with hundreds of tickets
# must not push the question itself out of the agent's attention.
MAX_TRACKER_TICKETS = 15
MAX_TRACKER_CHARS = 2000

# Turns kept when the caller does not trim. Long enough for a real thread,
# short enough that the prompt does not grow without bound.
MAX_HISTORY_TURNS = 20

_SYSTEM = """\
You are a sharp, friendly engineer having a conversation. Someone is thinking out \
loud with you — about an idea, a design question, how something works, or something \
they want to understand better. This is a discussion, not a work order.

How to be useful here:
- Answer the question that was actually asked, at the depth it was asked. A quick \
question gets a quick answer; a hard one earns real detail.
- Explain mechanisms, not just conclusions. "Why" is usually the interesting part.
- Have a view. If an idea has a problem, say so and say what you would do instead.
- Ask a question back only when the answer genuinely depends on it.
- Be concise by default — this is often read on a phone. Expand when the topic \
deserves it, and use short code snippets when they explain faster than prose.

Tools:
- If the conversation is about the user's own code and you have been given a \
project, read it before answering. Grounded beats plausible.
- If the question turns on something current — a library's present API, a recent \
release, what a tool does today — search rather than guessing from memory.

You cannot modify anything, and should not offer to. If the discussion arrives at \
something worth building, say so plainly in a sentence and let the user take it to \
the engineering team; do not write out the implementation.
"""


# Words that open a question or a request to explain. A message starting with
# one of these is a conversation even when it names a project — "how does
# resolvemind handle auth?" is a question about the code, not an order to change
# it.
_ASKING = (
    "how", "what", "why", "when", "where", "which", "who", "whose",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "should", "would", "will", "am",
    "explain", "compare", "describe", "tell", "help me understand",
    "any idea", "thoughts", "opinion", "wdyt",
)


def is_question(text: str) -> bool:
    """Is this a thing to discuss rather than a thing to build?

    Deliberately generous: mistaking a build request for a question costs one
    extra sentence, while mistaking a question for a build request puts a
    confirm dialog in front of someone who only wanted to know something.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    lowered = stripped.lower()
    first = lowered.split()[0].strip(",.!:;") if lowered.split() else ""
    return first in _ASKING or lowered.startswith(("help me understand", "any idea", "tell me"))


def _ticket_line(ticket: dict) -> str:
    money = f"${ticket['spent_usd']:.2f}/${ticket['budget_usd']:.2f}"
    if ticket.get("budget_exhausted"):
        money += " (budget spent)"
    line = (
        f"  #{ticket['id']} p{ticket.get('priority', '?')} "
        f"{str(ticket.get('title') or '')[:70]} — {money}"
    )
    verdict = ticket.get("review")
    if verdict:
        reasons = (verdict.get("reasons") or "")[:120]
        line += f" — review: {verdict['verdict']}" + (f" ({reasons})" if reasons else "")
    return line


def tracker_section(project_path: str, project_name: str = "") -> str:
    """The project's tracker as prompt text, or "" when there is nothing to say.

    "What is next on <project>?" is a tracker question, and the files cannot
    answer it. This reads the same board ``/backlog`` renders — one source, so
    the two surfaces cannot drift — and bounds what it puts in the prompt,
    because the question matters more than the hundredth ticket.
    """
    if not project_path:
        return ""
    from . import tickets

    try:
        board = tickets.board(project_path, limit=MAX_TRACKER_TICKETS)
    except Exception as exc:  # an unreadable tracker is not worth failing a chat over
        print(f"[discuss] tracker unavailable: {exc}", file=sys.stderr)
        return ""
    if not board["total"]:
        return ""

    state = board["wip"]
    head = [
        f"\n=== TICKET TRACKER — {project_name or project_path} ===",
        f"WIP {state['in_flight']}/{state['limit']} in flight "
        f"({state['slots']} slot(s) free).",
        "This is what the tracker says right now, as read-only context. You "
        "cannot move a ticket or start a run — say what you would do and let "
        "the user take it to the engineering team.",
    ]
    # Grouped the way the project itself is organised when it says so: an area
    # tells the agent what part of the product a ticket belongs to, which is
    # what "what is next on the website?" turns on. With no areas set there is
    # nothing to group by, so the lifecycle grouping stands.
    areas = {tickets.area_of(t) for t in board["tickets"]}
    lines: list[str] = []
    if areas - {tickets.UNASSIGNED}:
        head.append("Tickets are grouped by area — the part of the product they belong to.")
        for area in tickets.sort_areas(board["tickets"]):
            lines.append(f"[{area}]")
            lines.extend(
                _ticket_line(t) + f" — {t.get('status') or tickets.BACKLOG}"
                for t in board["tickets"] if tickets.area_of(t) == area
            )
    else:
        for status, items in board["groups"]:
            lines.append(f"[{status}]")
            lines.extend(_ticket_line(ticket) for ticket in items)

    # Drop tickets off the end until the whole section fits, and say how many
    # went: a truncation nobody can see is a lie about the queue's length.
    hidden = board["overflow"]
    budget = MAX_TRACKER_CHARS - len("\n".join(head)) - 80
    while lines and len("\n".join(lines)) > budget:
        if not lines[-1].startswith("["):
            hidden += 1
        lines.pop()
    footer = (
        [f"… +{hidden} more ticket(s) not shown — the dashboard has the rest."]
        if hidden else []
    )
    return "\n".join(head + lines + footer)


@dataclass
class DiscussReply:
    reply: str


def trim_history(history: list[dict], max_turns: int = MAX_HISTORY_TURNS) -> list[dict]:
    """Keep the most recent turns. Oldest go first."""
    if not history:
        return []
    return list(history[-max_turns:])


def _build_prompt(
    history: list[dict],
    message: str,
    project_name: str = "",
    project_context: str = "",
    project_path: str = "",
) -> str:
    parts = [_SYSTEM]

    if project_name:
        parts.append(
            f"\n=== PROJECT IN SCOPE ===\nThe user is asking in the context of "
            f"'{project_name}'. Its code is in your working directory — read it "
            f"when the question is about it."
        )
    if project_context:
        parts.append(f"\n=== PROJECT ORIENTATION ===\n{project_context}")

    tracker = tracker_section(project_path, project_name)
    if tracker:
        parts.append(tracker)

    if history:
        parts.append("\n=== CONVERSATION SO FAR ===")
        for turn in history:
            role = "USER" if turn.get("role") == "user" else "YOU"
            parts.append(f"{role}: {turn.get('content', '')}")

    parts.append(f"\n=== NEW MESSAGE ===\n{message}")
    return "\n".join(parts)


def discuss(
    message: str,
    history: Optional[list[dict]] = None,
    project_path: str = "",
    project_name: str = "",
    project_context: str = "",
) -> DiscussReply:
    """One turn of conversation.

    ``project_path`` is optional: with it the agent can read that repo, without it
    the conversation is simply not about a specific codebase. A scratch directory
    is used in that case so the tools have somewhere harmless to point.
    """
    prompt = _build_prompt(
        trim_history(history or []), message, project_name, project_context,
        project_path,
    )
    cwd = project_path or tempfile.gettempdir()

    result = call_llm(
        DISCUSS_PROVIDER,
        DISCUSS_MODEL,
        prompt,
        allowed_tools=DISCUSS_TOOLS,
        max_turns=DISCUSS_MAX_TURNS,
        cwd=cwd,
    )

    text = (result.text or "").strip()
    if not text:
        print("[discuss] empty reply from provider", file=sys.stderr)
    return DiscussReply(reply=text or "(no response)")
