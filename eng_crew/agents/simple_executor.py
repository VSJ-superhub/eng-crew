"""Single-shot agent for simple tasks — no architect, no HITL gate, no reviewer."""
from __future__ import annotations

import sys

from .base import BaseAgent
from .. import prompts
from ..providers import call_llm
from ..state import TeamState
from .. import tracker


def _progress_reporter(run_id: int, label: str):
    """Map CLI stream events onto tracker progress rows."""
    from ..providers.claude_cli import summarize_event

    def report(evt: dict) -> None:
        line = summarize_event(evt)
        if line:
            tracker.update_run_progress(run_id, -1, f"{label}: {line}")

    return report


class SimpleExecutorAgent(BaseAgent):
    agent_type = "simple_executor"

    def run(self, state: TeamState) -> dict:
        task = state.get("raw_task", "")
        project_path = state.get("project_path", ".")
        run_id = state.get("run_id", 0)

        print(f"[simple_execute] Single-shot for: {task[:80]!r}", file=sys.stderr)

        prompt = prompts.render("simple-edit", task=task)

        cfg = self.settings.get_agent_config("simple_executor")
        result = call_llm(
            cfg["provider"], cfg["model"], prompt,
            allowed_tools="Glob,Grep,Read,Edit,Write",
            max_turns=25,
            cwd=project_path,
            on_event=_progress_reporter(run_id, "simple"),
        )

        if run_id:
            try:
                tracker.log_event(run_id, -1, "simple_execute", result)
            except Exception as e:
                print(f"[tracker] log_event error: {e}", file=sys.stderr)

        text = (result.text or "Done").strip()
        summary = f"[simple] {text[:300]}"
        print(f"[simple_execute] Done: {summary[:120]}", file=sys.stderr)

        return {
            **state,
            "execution_results": [summary],
            "final_summary": summary,
            "cli_session_id": getattr(result, "session_id", "") or "",
        }
