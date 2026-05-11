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
    """Return 'simple', 'medium', or 'complex' based on task heuristics."""
    lower = task.lower()
    words = lower.split()
    score = 0

    if len(words) < 12:
        score -= 1
    elif len(words) > 40:
        score += 2
    elif len(words) > 20:
        score += 1

    for kw in _SIMPLE_KEYWORDS:
        if kw in lower:
            score -= 1
            break

    for kw in _COMPLEX_KEYWORDS:
        if kw in lower:
            score += 2

    connector_count = len(_COMPOUND_CONNECTORS.findall(task))
    if connector_count >= 3:
        score += 1

    file_mentions = len(_FILE_PATTERN.findall(task))
    if file_mentions >= 3:
        score += 1
    elif file_mentions == 0:
        score -= 1

    if task.count("\n\n") >= 2:
        score += 1

    if score <= 0:
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
        next_step = "simple" if tier == "simple" else "full"
        return {**state, "complexity_tier": tier, "_next": next_step}
