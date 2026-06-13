from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

from ..providers import call_llm, LLMResult
from .. import tracker


class BaseAgent:
    """Base class for all eng-crew agents."""
    agent_type: str = "generic"

    def __init__(self, settings: Any = None) -> None:
        self.settings = settings

    def call(
        self,
        prompt: str,
        role: Optional[str] = None,
        subtask_idx: int = 0,
        run_id: int = 0,
    ) -> LLMResult:
        role = role or self.agent_type
        if self.settings is not None:
            cfg = self.settings.get_agent_config(role)
        else:
            from ..config import settings as _global
            cfg = _global.get_agent_config(role)
        provider = cfg["provider"]
        model = cfg["model"]

        try:
            result = call_llm(provider, model, prompt)
            if run_id:
                tracker.log_event(run_id, subtask_idx, role, result)
            return result
        except Exception as e:
            print(f"[BaseAgent] Error in call ({role}): {e}", file=sys.stderr)
            raise

    def count_tokens(self, text: str) -> int:
        try:
            from entropy_engine import count_tokens as _ct
            return _ct(text)
        except ImportError:
            return len(text) // 4

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """Truncate text to max_tokens using BPE when available, char proxy otherwise."""
        try:
            from entropy_engine import count_tokens as _ct
            if _ct(text) <= max_tokens:
                return text
            lo, hi = 0, len(text)
            while hi - lo > 64:
                mid = (lo + hi) // 2
                if _ct(text[:mid]) <= max_tokens:
                    lo = mid
                else:
                    hi = mid
            return text[:lo] + "\n... [truncated]"
        except ImportError:
            cutoff = max_tokens * 4
            return text if len(text) <= cutoff else text[:cutoff] + "\n... [truncated]"

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return state
