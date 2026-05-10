from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from eng_crew.agents.base import BaseAgent
from eng_crew.providers import LLMResult


class SpecialistAgent(BaseAgent):
    agent_type: str = "specialist"
    file_extensions: list[str] = []

    def get_context_files(self, project_path: str, target_files: list[str]) -> str:
        parts: list[str] = []
        root = Path(project_path)
        for rel in target_files:
            candidate = root / rel
            if not candidate.exists():
                # try matching by extension if path not found
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
                parts.append(f"=== {rel} ===\n{content}")
            except OSError:
                continue
        return "\n\n".join(parts)

    def code(self, subtask: dict[str, Any], state: dict[str, Any]) -> str:
        project_path: str = state.get("project_path", "")
        target_files: list[str] = subtask.get("target_files") or []
        context = self.get_context_files(project_path, target_files)

        prompt = (
            f"You are a {self.agent_type} software engineer.\n\n"
            f"## Task\n{subtask.get('description', '')}\n\n"
        )
        if context:
            prompt += f"## Relevant Files\n{context}\n\n"
        prompt += (
            "## Instructions\n"
            "Output a unified diff patch that implements the task.\n"
            "Start with `--- a/<path>` / `+++ b/<path>` headers.\n"
            "Output ONLY the patch — no explanation, no markdown fences.\n"
        )

        result: LLMResult = self.call(
            prompt,
            role="coder",
            run_id=state.get("run_id") or 0,
            subtask_idx=subtask.get("index", 0),
        )
        return result.text
