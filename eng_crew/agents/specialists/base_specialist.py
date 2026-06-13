from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from eng_crew.agents.base import BaseAgent
from eng_crew.providers import LLMResult

_MAX_FILE_CHARS = 5000   # per-file cap; entropy_select refines the total after


class SpecialistAgent(BaseAgent):
    agent_type: str = "specialist"
    file_extensions: list[str] = []

    def _read_target_files(self, project_path: str, rel_paths: list[str]) -> str:
        """Read files and return assembled === path === blocks with per-file cap."""
        parts: list[str] = []
        root = Path(project_path)
        for rel in rel_paths:
            candidate = root / rel
            if not candidate.exists():
                parts.append(f"=== {rel} ===\n(does not exist yet)")
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
                if len(content) > _MAX_FILE_CHARS:
                    content = content[:_MAX_FILE_CHARS] + "\n... [truncated]"
                parts.append(f"=== {rel} ===\n{content}")
            except OSError:
                continue
        return "\n\n".join(parts)

    def _rag_files(self, project_path: str, query: str, top_k: int) -> list[str]:
        """Return relative paths of files relevant to query via LSH search."""
        try:
            from eng_crew import project_index
            return project_index.search(project_path, query, top_k=top_k)
        except Exception as e:
            print(f"[{self.agent_type}] RAG warning: {e}", file=sys.stderr)
            return []

    def get_context_files(self, project_path: str, target_files: list[str]) -> str:
        """Read target_files + RAG-surfaced files, apply preprocessor gates."""
        settings = self.settings
        if settings is None:
            from eng_crew.config import settings as _global
            settings = _global

        # ── RAG: surface related files beyond explicit targets ────────────────
        all_paths = list(target_files)
        if settings.rag_enabled and target_files:
            query = " ".join(target_files)   # file paths encode intent well
            hits = self._rag_files(project_path, query, top_k=settings.rag_top_k)
            seen = set(os.path.normpath(p) for p in all_paths)
            for hit in hits:
                norm = os.path.normpath(hit)
                if norm not in seen:
                    all_paths.append(hit)
                    seen.add(norm)
            cap = len(target_files) + settings.rag_top_k
            all_paths = all_paths[:cap]
            if len(all_paths) > len(target_files):
                print(
                    f"[{self.agent_type}] RAG: +{len(all_paths) - len(target_files)} files",
                    file=sys.stderr,
                )

        # ── Read files ────────────────────────────────────────────────────────
        contents = self._read_target_files(project_path, all_paths)

        # ── Preprocessor: AST extraction → entropy selection ─────────────────
        if settings.entropy_engine_enabled and contents:
            try:
                from eng_crew.preprocessor import ast_extract, entropy_select
                contents = ast_extract(contents)
                contents = entropy_select(contents, token_budget=settings.token_budget)
            except Exception as e:
                print(f"[{self.agent_type}] Preprocessor warning: {e}", file=sys.stderr)

        return contents

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
