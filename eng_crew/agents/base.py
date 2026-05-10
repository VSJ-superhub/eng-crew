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
        return len(text) // 4

    def run(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return state
