from __future__ import annotations

import json
import re
import sys

from eng_crew import prompts
from eng_crew.agents.base import BaseAgent
from eng_crew.state import Subtask, TeamState

def _extract_json(text: str) -> str:
    match = re.search(r"\{[\s\S]+\}", text)
    if match:
        return match.group(0)
    raise ValueError("No JSON object found in LLM response")


def _make_subtask(raw: dict) -> Subtask:
    return Subtask(
        id=str(raw["id"]),
        description=str(raw["description"]),
        target_files=[str(f) for f in raw.get("target_files", [])],
        agent_type=raw.get("agent_type") or "generic",
        patch=None,
        review_passed=None,
        review_feedback=None,
        retry_count=0,
        dependencies=[str(d) for d in raw.get("dependencies", [])],
        human_comment=None,
        clarification_question=None,
        clarification_response=None,
        status="pending",
        critic_feedback=None,
    )


class ArchitectAgent(BaseAgent):
    def decompose(self, state: TeamState) -> TeamState:
        prompt = prompts.render(
            "architect-decompose",
            project_context=state.get("project_context", ""),
            raw_task=state.get("raw_task", ""),
        )
        try:
            result = self.call(prompt, role="architect", run_id=state.get("run_id") or 0)
            raw_json = _extract_json(result.text)
            data = json.loads(raw_json)
            subtasks = [_make_subtask(s) for s in data["subtasks"]]
        except Exception as exc:
            print(f"[ArchitectAgent] decompose failed: {exc}", file=sys.stderr)
            raise

        return {**state, "subtasks": subtasks, "_next": "dispatcher"}
