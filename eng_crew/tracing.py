"""What a run did, tool by tool.

The Claude CLI streams a structured event for every tool the agent uses and
for its result. Every execution tier already passes an ``on_event`` callback to
read a progress line out of that stream; this module is the other half — the
same callback also writes one ``tool_call_events`` row per finished tool call,
so a run has a trace, not just a last-known activity string.

One handler does both jobs because there is only one callback seam, and a tier
that traced but stopped reporting progress (or the reverse) would be a
regression. Nothing here can fail a run: a trace is an observation of the work,
never a step in it, so every error is swallowed and printed.
"""
from __future__ import annotations

import sys
from typing import Callable

from . import tracker


def run_event_handler(run_id: int, label: str = "", subtask_idx: int = -1) -> Callable:
    """An ``on_event`` callback that records a run's tool calls.

    ``label`` names the tier in the progress line ("single-agent: Edit a.py");
    pass none for a call that should be traced but not narrated — the ticket
    reviewer, whose activity is not the run's progress.
    """
    from .providers.claude_cli import (
        TOOL_CALL_EVENT,
        narration_text,
        summarize_event,
    )

    def report(evt: dict) -> None:
        try:
            if evt.get("type") == TOOL_CALL_EVENT:
                tracker.log_tool_call_event(
                    run_id, subtask_idx,
                    evt.get("tool_name") or "tool",
                    evt.get("started_at") or "",
                    int(evt.get("duration_ms") or 0),
                    evt.get("outcome") or "unknown",
                    evt.get("target") or "",
                )
                return
            if label:
                # What the agent said, kept as its own row. The progress line
                # holds only the latest one; the story needs all of them. Tied
                # to `label` for the same reason the progress line is: a
                # reviewer's commentary is not the run's own narration.
                said = narration_text(evt)
                if said:
                    tracker.log_run_narration(run_id, subtask_idx, said)
                line = summarize_event(evt)
                if line:
                    tracker.update_run_progress(run_id, subtask_idx, f"{label}: {line}")
        except Exception as exc:
            print(f"[tracing] could not record event: {exc}", file=sys.stderr)

    return report
