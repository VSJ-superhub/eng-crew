"""Heuristic complexity classifier — runs before orchestrator to pick the optimal path."""
from __future__ import annotations

import re
import sys
from typing import Any

from .base import BaseAgent
from ..state import TeamState


_SIMPLE_KEYWORDS = {
    "fix", "typo", "rename", "remove", "delete", "update", "change",
    "tweak", "minor", "small", "bump", "correct", "adjust", "add field",
    "add column", "add property",
}
_COMPLEX_KEYWORDS = {
    "refactor", "migrate", "rewrite", "redesign", "overhaul", "rebuild",
    "restructure", "architect", "pipeline", "system", "integration",
    "multi-tenant", "authentication", "authorization", "workflow",
}
_COMPOUND_CONNECTORS = re.compile(
    r"\b(and|also|then|additionally|furthermore|plus|as well as)\b|;",
    re.IGNORECASE,
)
_FILE_PATTERN = re.compile(
    r"\b\w[\w\-]*\.(py|ts|tsx|js|jsx|yaml|yml|md|json|sql|go|rs|java)\b",
    re.IGNORECASE,
)


def classify(task: str) -> str:
    """Return 'simple', 'medium', or 'complex' from task heuristics.

    Tuned for this project's dispatch convention: short, intent-driven tasks that
    deliberately DO NOT name file paths (the architect derives them). Absence of
    file mentions is therefore the *norm* and is not treated as a simplicity
    signal. The bands are biased so the default (no strong signal) lands in
    'medium' -> the single-agent tier: a task must actively look trivial to fall to
    'simple' (a small-edit verb on a short prompt), or look broad/compound to reach
    'complex' (the full multi-agent graph). Erring toward 'medium' is cheap (one
    capable call); under-powering a real feature as 'simple' is the costlier miss.
    """
    lower = task.lower()
    words = lower.split()
    n = len(words)
    score = 0

    # Length: only very short prompts read as trivial; long ones read as broad.
    # The length bumps are cumulative (25 -> +1, 45 -> +2, 70 -> +3).
    if n < 7:
        score -= 1
    if n >= 25:
        score += 1
    if n >= 45:
        score += 1
    if n >= 70:
        score += 1

    # A small-edit verb nudges toward 'simple', but only on a short prompt — a
    # long task that merely contains "update"/"change" is not therefore trivial.
    if n < 16:
        for kw in _SIMPLE_KEYWORDS:
            if kw in lower:
                score -= 1
                break

    # Each complex-domain keyword is a strong breadth signal.
    for kw in _COMPLEX_KEYWORDS:
        if kw in lower:
            score += 2

    # Several connectors imply multiple independent pieces of work.
    if len(_COMPOUND_CONNECTORS.findall(task)) >= 3:
        score += 1

    # Explicit multi-file mentions imply breadth. Absence is NOT penalised.
    file_mentions = len(_FILE_PATTERN.findall(task))
    if file_mentions >= 5:
        score += 2
    elif file_mentions >= 3:
        score += 1

    # Multi-paragraph tasks are inherently broad.
    if task.count("\n\n") >= 2:
        score += 1

    # Trivial must be actively earned (short + a small-edit verb ~= -2); the
    # default falls through to 'medium'; broad/compound signals reach 'complex'.
    if score <= -2:
        return "simple"
    if score <= 3:
        return "medium"
    return "complex"


class ComplexityClassifierAgent(BaseAgent):
    agent_type = "classifier"

    def run(self, state: TeamState) -> dict:
        task = state.get("raw_task", "")
        tier = classify(task)
        print(f"[complexity] tier={tier!r} for task: {task[:80]!r}", file=sys.stderr)
        # simple  -> single-shot executor (trivial edits)
        # medium  -> single-agent tier (one capable call, plans+codes+tests)
        # complex -> full multi-agent graph (architect/critic/HITL/reviewer fan-out)
        next_step = {"simple": "simple", "medium": "single", "complex": "full"}[tier]
        return {**state, "complexity_tier": tier, "_next": next_step}
