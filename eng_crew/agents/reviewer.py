from __future__ import annotations

import logging

from eng_crew.agents.base import BaseAgent
from eng_crew.config import Settings
from eng_crew.state import Subtask, TeamState

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

REVIEW_PROMPT = """\
You are a senior code reviewer. Review the following patch for a software subtask.

## Subtask Description
{description}

## Target Files
{target_files}

## Patch
```diff
{patch}
```

Review for:
- Correctness: does it implement what the description requires?
- Safety: no security vulnerabilities, no destructive side effects
- Completeness: all target files addressed, no missing logic
- Quality: follows project conventions, no unnecessary changes

Respond with exactly one of:
APPROVED — the patch is correct and ready to apply
RETRY — the patch has issues that must be fixed

If RETRY, add a brief explanation on the next line describing what must be fixed.
Your first word MUST be either APPROVED or RETRY.
"""


class ReviewerAgent(BaseAgent):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def review(self, state: TeamState) -> TeamState:
        subtasks: list[Subtask] = list(state["subtasks"])
        run_id: int = state.get("run_id") or 0
        idx: int = state.get("current_subtask_idx", 0)

        # Find the coded subtask — prefer current_subtask_idx, fall back to search
        subtask: Subtask | None = None
        actual_idx: int = idx
        if 0 <= idx < len(subtasks) and subtasks[idx]["status"] == "coded":
            subtask = subtasks[idx]
        else:
            for i, s in enumerate(subtasks):
                if s["status"] == "coded":
                    subtask = s
                    actual_idx = i
                    break

        if subtask is None:
            logger.warning("ReviewerAgent: no coded subtask found — routing to orchestrator")
            return {**state, "_next": "orchestrator"}

        patch = subtask.get("patch") or ""
        description = subtask.get("description", "")
        target_files = ", ".join(subtask.get("target_files") or [])

        prompt = REVIEW_PROMPT.format(
            description=description,
            target_files=target_files or "(none specified)",
            patch=patch or "(empty patch)",
        )

        try:
            result = self.call(prompt, role="reviewer", run_id=run_id, subtask_idx=actual_idx)
            response_text: str = result.text.strip()
        except Exception:
            logger.exception("ReviewerAgent: LLM call failed for subtask %s", subtask["id"])
            # Treat LLM failure as a retry-worthy event
            response_text = "RETRY\nLLM call failed during review."

        first_word = response_text.split()[0].upper() if response_text else "RETRY"
        feedback_lines = response_text.split("\n", 1)
        feedback = feedback_lines[1].strip() if len(feedback_lines) > 1 else ""

        retry_count: int = subtask.get("retry_count", 0)

        if first_word == "APPROVED":
            logger.info("ReviewerAgent: subtask %s APPROVED", subtask["id"])
            updated_subtask: Subtask = {
                **subtask,
                "status": "reviewed",
                "review_passed": True,
                "review_feedback": feedback,
            }
            subtasks[actual_idx] = updated_subtask
            return {
                **state,
                "subtasks": subtasks,
                "current_subtask_idx": actual_idx,
                "_next": "executor",
            }

        # RETRY path
        logger.info(
            "ReviewerAgent: subtask %s RETRY (attempt %d/%d): %s",
            subtask["id"],
            retry_count + 1,
            MAX_RETRIES,
            feedback,
        )

        if retry_count < MAX_RETRIES:
            updated_subtask = {
                **subtask,
                "status": "pending",
                "retry_count": retry_count + 1,
                "review_passed": False,
                "review_feedback": feedback,
            }
            subtasks[actual_idx] = updated_subtask
            return {
                **state,
                "subtasks": subtasks,
                "current_subtask_idx": actual_idx,
                "_next": "dispatcher",
            }

        # Max retries exceeded
        logger.warning(
            "ReviewerAgent: subtask %s exceeded max retries (%d) — marking failed",
            subtask["id"],
            MAX_RETRIES,
        )
        updated_subtask = {
            **subtask,
            "status": "failed",
            "review_passed": False,
            "review_feedback": feedback,
        }
        subtasks[actual_idx] = updated_subtask
        failed_count: int = state.get("failed_subtask_count", 0) + 1
        return {
            **state,
            "subtasks": subtasks,
            "current_subtask_idx": actual_idx,
            "failed_subtask_count": failed_count,
            "_next": "orchestrator",
        }
