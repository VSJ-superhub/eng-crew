from __future__ import annotations

from ..agents.base import BaseAgent
from ..state import TeamState


MAX_FAILED_SUBTASKS = 3
# Hard ceiling on orchestrator cycles — backstop against any termination gap so a
# stuck run ends with a clean summary rather than a LangGraph recursion error.
MAX_ORCHESTRATOR_LOOPS = 50


class OrchestratorAgent(BaseAgent):
    def run(self, state: TeamState) -> TeamState:
        loops = state.get("orchestrator_loops", 0)
        if loops == 0:
            return {**state, "orchestrator_loops": 1, "_next": "architect"}

        subtasks = state.get("subtasks") or []
        failed_count = state.get("failed_subtask_count", 0)

        # Track failed IDs so permanently-blocked subtasks can be detected.
        # A subtask is permanently blocked when any of its deps is failed OR is
        # itself permanently blocked — i.e. a dep can never reach "done". We compute
        # the transitive closure so multi-level dependency chains above a failed
        # subtask are also detected (A failed -> B deps A -> C deps B all block).
        # NOTE: active_subtask_ids is intentionally not used here — stale active IDs
        # from a previous cycle would wrongly block all remaining candidates.
        by_id = {s["id"]: s for s in subtasks}
        blocked_ids = {s["id"] for s in subtasks if s.get("status") == "failed"}
        # Iterate to a fixpoint: a subtask becomes blocked once any dep is blocked.
        changed = True
        while changed:
            changed = False
            for s in subtasks:
                if s["id"] in blocked_ids or s.get("status") == "done":
                    continue
                if any(d in blocked_ids for d in (s.get("dependencies") or [])):
                    blocked_ids.add(s["id"])
                    changed = True

        def _is_resolved(s: dict) -> bool:
            return s.get("status") in ("done", "failed") or s["id"] in blocked_ids

        all_resolved = bool(subtasks) and all(_is_resolved(s) for s in subtasks)
        all_done = bool(subtasks) and all(s.get("status") == "done" for s in subtasks)
        over_limit = failed_count >= MAX_FAILED_SUBTASKS
        loop_exhausted = loops >= MAX_ORCHESTRATOR_LOOPS

        if all_resolved or over_limit or loop_exhausted:
            done_count = sum(1 for s in subtasks if s.get("status") == "done")
            failed_descs = [
                s.get("description", s.get("id", "unknown"))
                for s in subtasks
                if s.get("status") != "done"
            ]

            if all_done:
                summary = (
                    f"All {done_count} subtask(s) completed successfully."
                )
            elif over_limit and not all_resolved:
                reason = f"Stopping after {failed_count} failed subtasks."
                failed_note = (
                    f" Failed: {', '.join(failed_descs)}." if failed_descs else ""
                )
                summary = (
                    f"{reason}{failed_note} "
                    f"Completed {done_count}/{len(subtasks)} subtasks."
                )
            elif loop_exhausted and not all_resolved:
                summary = (
                    f"Stopping after {loops} orchestrator cycles (loop ceiling reached). "
                    f"Completed {done_count}/{len(subtasks)} subtasks. "
                    f"Unresolved: {', '.join(failed_descs)}."
                )
            else:
                blocked = [
                    s.get("description", s.get("id", "unknown"))
                    for s in subtasks
                    if s.get("status") != "done"
                ]
                summary = (
                    f"Completed {done_count}/{len(subtasks)} subtasks. "
                    f"Remaining subtasks permanently blocked by failed dependencies: "
                    f"{', '.join(blocked)}."
                )

            return {**state, "_next": "done", "final_summary": summary}

        return {
            **state,
            "orchestrator_loops": loops + 1,
            "_next": "dispatcher",
        }
