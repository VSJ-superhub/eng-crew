from __future__ import annotations

import json
import re
import sys

from eng_crew.agents.base import BaseAgent
from eng_crew.state import Subtask, TeamState

_PROMPT_TEMPLATE = """\
You are a senior software architect. Decompose the following task into discrete, parallelizable subtasks.

PROJECT CONTEXT:
{project_context}

TASK:
{raw_task}

Respond with ONLY valid JSON in this exact format — no prose, no markdown fences:
{{
  "subtasks": [
    {{
      "id": "s1",
      "description": "...",
      "target_files": ["path/to/file.py"],
      "agent_type": "backend",
      "dependencies": []
    }}
  ]
}}

agent_type must be one of: architect, critic, backend, frontend, database, ai_pipeline, infrastructure, generic.
dependencies is a list of subtask ids that must complete before this one starts.
target_files contains paths relative to the project root that this subtask will create or modify.
"""


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
        prompt = _PROMPT_TEMPLATE.format(
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
