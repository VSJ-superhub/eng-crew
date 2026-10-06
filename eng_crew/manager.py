"""Grounded Manager (PM) agent — ideates against the real codebase, then writes tickets.

Surface-agnostic: the dashboard, Discord, and a CLI are thin frontends. The manager
holds a conversation (history is passed in per turn), explores the *actual* code with
read-only tools while it ideates, and — when a concrete, buildable unit of work has
emerged — proposes it as a ticket: title, description, acceptance criteria. On the
human's accept, :func:`accept` files that ticket in the project's backlog.

Filing a ticket starts nothing. The manager is the product manager, not the dispatcher:
a human moving the ticket to ``ready`` is the approval, and the existing ticket queue
builds it from there. That is why there is no dispatch here any more — a free-text task
fired straight at the builder skipped both the approval and the definition of done.

A task dispatched with no acceptance criteria goes through the manager too, without
the conversation: :func:`draft_ticket` writes the definition of done from the task and
the real code. It still starts nothing — the dispatcher readies the ticket.

Design invariants:
  - The manager is GROUNDED: claude_cli with Read/Grep/Glob and cwd=project, so its
    ideas reflect the real code, not a vacuum. (Contrast the old dashboard intake,
    which ran with --allowedTools none --max-turns 1.)
  - The manager NEVER writes code and never starts a run. It has read-only tools;
    building is a separate, explicit, human-approved step.
  - A proposal with no acceptance criteria still becomes a ticket, but the gap is
    handed back to the surface to show — the lifecycle will refuse to make such a
    ticket ready, and a silent un-buildable ticket is worse than a visible one.
  - Ideation runs on the Claude subscription (claude_cli) — effectively flat cost.
    Metered spend happens only when a ticket is built.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from .providers import call_llm

# Grounded ideation: strong reasoning, read-only tools, room to explore a few files.
MANAGER_PROVIDER = "claude_cli"
MANAGER_MODEL = "claude-sonnet-5"
MANAGER_TOOLS = "Read,Grep,Glob"
MANAGER_MAX_TURNS = 10

_SYSTEM = """You are a senior engineering program manager and tech lead. A product person (the "program manager") talks to you in natural language about what they want. Your job is to IDEATE WITH THEM and turn fuzzy intent into one concrete, buildable TICKET — grounded in the real codebase.

How you work:
- Ground every idea in reality. Use Read, Grep, and Glob to inspect the actual project before you propose anything. Reference what you find ("you already have X in file Y, so this would hook in there").
- Ask at most 1-2 focused clarifying questions at a time. Do not interrogate.
- Be concise. This is a fast planning conversation on a phone or a chat panel, not a doc.
- You do NOT write code. A ticket you write goes to the project's backlog; a human approves it, and the engineering team then implements it on an isolated branch.
- Propose ONE ticket at a time — the smallest coherent slice that delivers value.

When (and only when) you and the user have converged on something concrete AND the user signals they want it written up, end your reply with EXACTLY one fenced block:

```ticket
{"title": "<short imperative title>", "description": "<what the work is, in a sentence or two, self-contained enough to act on without further questions>", "acceptance_criteria": ["<observable condition that must hold when this is done>", "..."], "rationale": "<one sentence: why this, and how it fits the code>"}
```

Rules for the ticket block:
- Emit it only when the user is ready to write the ticket up — not while still exploring options.
- ALWAYS include acceptance_criteria: concrete, checkable statements about behaviour, not restatements of the title. A ticket with no acceptance criteria cannot be approved for build, so leaving them out blocks the work.
- The title and description must be specific and intent-level (name the outcome), but must NOT invent file paths or APIs you did not verify by reading the code.
- Never emit more than one ticket block. If you are still ideating, do not emit one at all."""

_TICKET_RE = re.compile(r"```ticket\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class ManagerReply:
    """One manager turn. `proposal` is set only when a ticket is ready to accept."""
    reply: str
    #: {"title", "description", "acceptance_criteria": list[str], "rationale"}
    proposal: Optional[dict] = None


#: What a surface tells the user when the manager wrote a ticket with no definition
#: of done. Such a ticket exists, but the lifecycle will not let it become ready.
MISSING_CRITERIA_WARNING = (
    "This ticket has no acceptance criteria, so it cannot be approved for build. "
    "Add them on the ticket (or ask the manager for them) before moving it to ready."
)


def _criteria_list(raw: Any) -> list[str]:
    """Normalise acceptance criteria to a list of non-empty lines."""
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for item in raw:
        text = str(item).strip().lstrip("-*").strip()
        if text:
            out.append(text)
    return out


def criteria_text(criteria: Any) -> str:
    """Acceptance criteria as the bulleted text a ticket stores."""
    return "\n".join(f"- {c}" for c in _criteria_list(criteria))


def _extract_proposal(text: str) -> tuple[str, Optional[dict]]:
    """Split a manager response into (conversational reply, optional ticket proposal)."""
    m = _TICKET_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return (text or "").strip(), None
    reply = ((text[: m.start()] + text[m.end():]) or "").strip()
    # "task" is what the manager used to emit, before a proposal was a ticket.
    title = str(obj.get("title") or obj.get("task") or "").strip()
    if not title:
        return reply, None
    return reply, {
        "title": title,
        "description": str(obj.get("description") or "").strip(),
        "acceptance_criteria": _criteria_list(obj.get("acceptance_criteria")),
        "rationale": str(obj.get("rationale", "")).strip(),
    }


def _build_prompt(history: list[dict], message: str, project_context: str, vision: str) -> str:
    parts = [_SYSTEM]
    if vision:
        parts.append(
            "\n=== REMEMBERED PRODUCT VISION (from prior conversations; your evolving "
            "understanding of what the user wants — build on it, refine it) ===\n" + vision
        )
    if project_context:
        parts.append(f"\n=== PROJECT ORIENTATION (for context; verify against the code) ===\n{project_context}")
    if history:
        parts.append("\n=== CONVERSATION SO FAR ===")
        for m in history:
            role = "USER" if m.get("role") == "user" else "MANAGER"
            parts.append(f"{role}: {m.get('content', '')}")
    parts.append(f"\n=== NEW USER MESSAGE ===\n{message}")
    parts.append("\nRespond concisely as the program manager. Explore the code with your tools when it helps ground the idea.")
    return "\n".join(parts)


def chat(
    message: str,
    history: Optional[list[dict]] = None,
    project_path: str = ".",
    project_context: str = "",
) -> ManagerReply:
    """One grounded manager turn.

    Args:
        message:         the user's new message.
        history:         prior turns as [{"role": "user"|"assistant"|"manager", "content": str}].
        project_path:    repo root — becomes claude_cli's cwd so Read/Grep/Glob hit real files.
        project_context: optional orientation text (rendered project summary).

    Returns a ManagerReply(reply, proposal).
    """
    from . import vision as _vision
    remembered = _vision.get_vision(project_path)
    prompt = _build_prompt(history or [], message, project_context, remembered)
    result = call_llm(
        MANAGER_PROVIDER,
        MANAGER_MODEL,
        prompt,
        allowed_tools=MANAGER_TOOLS,
        max_turns=MANAGER_MAX_TURNS,
        cwd=project_path,
    )
    reply, proposal = _extract_proposal(result.text or "")
    return ManagerReply(reply=reply or "(no response)", proposal=proposal)


_DRAFT_SYSTEM = """You are a senior engineering program manager and tech lead. Someone has handed the engineering team a task in free text, with no definition of done. Your job is to turn it into ONE buildable ticket, grounded in the real codebase.

How you work:
- Read the actual project first with Read, Grep, and Glob. The criteria must be checkable against this code, not against a guess.
- Do not ask questions and do not converse — there is nobody to answer you. Commit to the most reasonable reading of the task.
- Do not widen the task. The ticket is the work as asked, with the finish line made explicit.
- You do NOT write code.

Reply with EXACTLY one fenced block and nothing else:

```ticket
{"title": "<short imperative title>", "description": "<what the work is, in a sentence or two, self-contained enough to act on>", "acceptance_criteria": ["<observable condition that must hold when this is done>", "..."], "rationale": "<one sentence: how this fits the code you read>"}
```

acceptance_criteria is mandatory and must be concrete, checkable statements about behaviour — not restatements of the title. A ticket with no acceptance criteria cannot be approved for build, so leaving them out blocks the work."""


class DraftFailed(RuntimeError):
    """The manager did not return a usable ticket for a task."""


def draft_ticket(task: str, project_path: str = ".", project_context: str = "") -> dict:
    """Draft a ticket for a free-text task, grounded in the project's code.

    This is the manager doing its job for a caller that skipped the ideation
    conversation — `run_task` with no acceptance criteria given. Same grounded,
    read-only agent, one shot instead of a dialogue: a task dispatched with no
    definition of done cannot be reviewed against anything, and the lifecycle
    will not make such a ticket ready.

    Returns a proposal dict in the shape :func:`accept` takes. Raises
    :class:`DraftFailed` if the manager returned no usable ticket — the caller
    must then supply acceptance criteria itself rather than get a ticket nobody
    can judge.
    """
    text = str(task or "").strip()
    if not text:
        raise DraftFailed("there is no task to draft a ticket from")

    parts = [_DRAFT_SYSTEM]
    if project_context:
        parts.append(
            f"\n=== PROJECT ORIENTATION (for context; verify against the code) ===\n{project_context}"
        )
    parts.append(f"\n=== THE TASK AS IT WAS HANDED TO THE TEAM ===\n{text}")
    result = call_llm(
        MANAGER_PROVIDER,
        MANAGER_MODEL,
        "\n".join(parts),
        allowed_tools=MANAGER_TOOLS,
        max_turns=MANAGER_MAX_TURNS,
        cwd=project_path,
    )
    _, proposal = _extract_proposal(result.text or "")
    if not proposal:
        raise DraftFailed("the manager returned no ticket for this task")
    # A drafted ticket with no criteria is the one thing this call exists to
    # produce, so an empty list is a failure here rather than a warning.
    if not proposal["acceptance_criteria"]:
        raise DraftFailed("the manager drafted no acceptance criteria for this task")
    return proposal


def accept(proposal: dict, project_path: str, *, actor: str = "manager",
           claude_md_path: str = "", project_id: int | None = None,
           priority: int = 50) -> dict:
    """File an accepted proposal as a ticket in the project's backlog.

    This is where the manager's job ends. No run is started: the ticket lands in
    ``backlog``, and a human moving it to ``ready`` is the approval that releases
    it into the queue. The ticket's first event names ``actor`` (the manager), so
    its history shows who wrote it.

    Returns ``{"ticket_id", "ticket", "needs_acceptance_criteria", "warning"}``.
    A proposal with no acceptance criteria is still filed — losing the write
    would lose the thinking — but ``warning`` is non-empty so the surface can say
    so rather than leaving an un-approvable ticket sitting silently in the backlog.
    """
    from . import tracker

    title = str((proposal or {}).get("title") or (proposal or {}).get("task") or "").strip()
    if not title:
        raise ValueError("a proposal needs a title before it can become a ticket")

    criteria = criteria_text((proposal or {}).get("acceptance_criteria"))
    description = str((proposal or {}).get("description") or "").strip() or title
    rationale = str((proposal or {}).get("rationale") or "").strip()
    if rationale:
        description = f"{description}\n\nWhy: {rationale}"

    ticket_id = tracker.add_backlog_item(
        title=title[:200],
        description=description,
        project_path=project_path,
        claude_md_path=claude_md_path,
        priority=priority,
        project_id=project_id,
        item_type="feature",
        acceptance_criteria=criteria,
        created_by=actor,
    )

    # Remember the accepted work as part of the product vision.
    try:
        from . import vision as _vision
        _vision.record_build(project_path, title)
    except Exception:
        pass

    return {
        "ticket_id": ticket_id,
        "ticket": tracker.get_backlog_item(ticket_id) or {},
        "needs_acceptance_criteria": not criteria,
        "warning": "" if criteria else MISSING_CRITERIA_WARNING,
    }


def status(run_id: int) -> Optional[dict]:
    """Current status/detail for a dispatched build."""
    from . import tracker
    return tracker.get_run_detail(run_id)


def remember(project_path: str, history: list[dict], project_name: str = "") -> str:
    """Distill an ideation conversation into the project's persistent vision memory.

    Call this at session end (e.g. Discord /endideate, dashboard reset). Blocking —
    surfaces should run it on a background thread. Returns the updated vision text.
    """
    from . import vision as _vision
    return _vision.remember(project_path, history or [], project_name)
