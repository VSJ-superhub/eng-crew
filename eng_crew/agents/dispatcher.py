from __future__ import annotations

import logging
from typing import Optional

from eng_crew import tracker
from eng_crew.agents.base import BaseAgent
from eng_crew.agents.specialists.ai_pipeline import AIPipelineAgent
from eng_crew.agents.specialists.backend import BackendAgent
from eng_crew.agents.specialists.database import DatabaseAgent
from eng_crew.agents.specialists.frontend import FrontendAgent
from eng_crew.agents.specialists.generic import GenericAgent
from eng_crew.agents.specialists.infrastructure import InfrastructureAgent
from eng_crew.config import Settings
from eng_crew.state import Subtask, TeamState

logger = logging.getLogger(__name__)

_SPECIALIST_REGISTRY: dict[str, type] = {
    "backend": BackendAgent,
    "frontend": FrontendAgent,
    "database": DatabaseAgent,
    "ai_pipeline": AIPipelineAgent,
    "infrastructure": InfrastructureAgent,
    "generic": GenericAgent,
}


class DispatcherAgent(BaseAgent):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def _dependencies_met(self, subtask: Subtask, subtasks: list[Subtask]) -> bool:
        done_ids = {s["id"] for s in subtasks if s["status"] == "done"}
        return all(dep in done_ids for dep in subtask.get("dependencies", []))

    def _find_next_pending(self, subtasks: list[Subtask]) -> Optional[tuple[int, Subtask]]:
        for idx, subtask in enumerate(subtasks):
            if subtask["status"] != "pending":
                continue
            if self._dependencies_met(subtask, subtasks):
                return idx, subtask
        return None

    def dispatch(self, state: TeamState) -> TeamState:
        subtasks: list[Subtask] = list(state["subtasks"])
        run_id: int = state.get("run_id") or 0

        result = self._find_next_pending(subtasks)
        if result is None:
            logger.info("No pending subtask with satisfied dependencies found.")
            return {**state, "_next": "reviewer"}

        idx, subtask = result
        agent_type: str = subtask.get("agent_type") or "generic"
        specialist_cls = _SPECIALIST_REGISTRY.get(agent_type, GenericAgent)
        specialist = specialist_cls(self.settings)

        logger.info("Dispatching subtask %s (agent_type=%s): %s", subtask["id"], agent_type, subtask["description"])

        tracker.update_run_progress(run_id, idx, subtask["description"])

        try:
            patch: str = specialist.code(subtask, state)
            updated_subtask: Subtask = {**subtask, "patch": patch, "status": "coded"}
        except Exception:
            logger.exception("Specialist failed on subtask %s", subtask["id"])
            updated_subtask = {**subtask, "patch": None, "status": "failed"}

        subtasks[idx] = updated_subtask

        return {
            **state,
            "subtasks": subtasks,
            "current_subtask_idx": idx,
            "_next": "reviewer",
        }
