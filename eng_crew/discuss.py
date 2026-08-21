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
        trim_history(history or []), message, project_name, project_context
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
