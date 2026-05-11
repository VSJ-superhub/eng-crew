"""Single-shot agent for simple tasks — no architect, no HITL gate, no reviewer."""
from __future__ import annotations

import sys

from .base import BaseAgent
from ..providers import call_llm
from ..state import TeamState
from .. import tracker


class SimpleExecutorAgent(BaseAgent):
    agent_type = "simple_executor"

    def run(self, state: TeamState) -> dict:
        task = state.get("raw_task", "")
        project_path = state.get("project_path", ".")
        run_id = state.get("run_id", 0)

        print(f"[simple_execute] Single-shot for: {task[:80]!r}", file=sys.stderr)

        prompt = f"""You are making a small, focused change to a codebase. No planning, no subtasks — read the relevant files and implement the change directly.

=== TASK ===
{task}

Use Glob, Grep, and Read to understand the codebase, then Edit or Write to implement the change.
Be minimal — only change what the task requires. When done, briefly describe what you changed.
"""

        cfg = self.settings.get_agent_config("simple_executor")
        result = call_llm(
            cfg["provider"], cfg["model"], prompt,
            allowed_tools="Glob,Grep,Read,Edit,Write",
            max_turns=10,
            cwd=project_path,
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
        }
