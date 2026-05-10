from __future__ import annotations

from ..agents.base import BaseAgent
from ..state import TeamState


MAX_FAILED_SUBTASKS = 3


class OrchestratorAgent(BaseAgent):
    def run(self, state: TeamState) -> TeamState:
        loops = state.get("orchestrator_loops", 0)
        if loops == 0:
            return {**state, "orchestrator_loops": 1, "_next": "architect"}

        subtasks = state.get("subtasks") or []
        failed_count = state.get("failed_subtask_count", 0)

        all_done = bool(subtasks) and all(
            s.get("status") == "done" for s in subtasks
        )
        over_limit = failed_count >= MAX_FAILED_SUBTASKS

        if all_done or over_limit:
            done_count = sum(1 for s in subtasks if s.get("status") == "done")
            failed_descs = [
                s.get("description", s.get("id", "unknown"))
                for s in subtasks
                if s.get("status") != "done"
            ]

            if over_limit and not all_done:
                reason = f"Stopping after {failed_count} failed subtasks."
                failed_note = (
                    f" Failed: {', '.join(failed_descs)}." if failed_descs else ""
                )
                summary = (
                    f"{reason}{failed_note} "
                    f"Completed {done_count}/{len(subtasks)} subtasks."
                )
            else:
                summary = (
                    f"All {done_count} subtask(s) completed successfully."
                )

            return {**state, "_next": "done", "final_summary": summary}

        return {
            **state,
            "orchestrator_loops": loops + 1,
            "_next": "dispatcher",
        }
