"""Single-agent tier for medium tasks — one capable CLI call that plans, codes,
and tests in a single context. Skips the architect → critic → HITL → dispatcher →
reviewer → executor fan-out, which pays a context tax at every provider hop.

This is the middle tier between `simple_executor` (trivial edits) and the full
graph (genuinely large, parallelizable work). Most everyday "medium" tasks need
one capable agent editing directly, not a multi-agent decomposition.
"""
from __future__ import annotations

import sys

from .base import BaseAgent
from .. import prompts, tickets
from ..providers import call_llm
from ..state import TeamState
from .. import tracker


def _progress_reporter(run_id: int, label: str):
    """Map CLI stream events onto progress rows and the run's tool-call trace."""
    from ..tracing import run_event_handler

    return run_event_handler(run_id, label)


class SingleAgentEngineer(BaseAgent):
    agent_type = "single_agent"

    # Budget for the project-context orientation block (kept lean on purpose).
    CONTEXT_TOKEN_BUDGET = 1200
    # Long-horizon agentic runs need room; a low cap silently truncates the
    # implementation (see claude_cli's max-turns soft-stop). These are the
    # fallbacks when settings do not say (see Settings.builder_*max_turns).
    MAX_TURNS = 60
    TEST_FIRST_MAX_TURNS = 100

    # Budget for the past-ticket retros carried into the build. Small: they are
    # hints from finished work, and must not crowd out the task or the code.
    LESSONS_TOKEN_BUDGET = 600

    # Budget for the acceptance criteria quoted back into the test-first block.
    # Generous — criteria are the definition of done and truncating one loses a
    # test; a runaway description still cannot swamp the prompt.
    CRITERIA_TOKEN_BUDGET = 1500

    def _test_first_block(self, task: str) -> str:
        """The test-first directive, when the task carries acceptance criteria.

        Criteria are a definition of done written before the code exists, which
        is exactly what a test is. Tickets carry them, so ticket-driven runs get
        told to encode them as failing tests first; a bare task string has no
        such definition and gets the ordinary approach.
        """
        criteria = tickets.acceptance_criteria_of(task)
        if not criteria:
            return ""
        criteria = self.truncate_to_tokens(criteria, self.CRITERIA_TOKEN_BUDGET)
        print("[single_agent] acceptance criteria present — test-first mode", file=sys.stderr)
        return "\n" + prompts.render("test-first", criteria=criteria) + "\n"

    def _lessons_block(self, state: TeamState) -> str:
        """Retros from tickets this project already shipped.

        The lessons store only pays for itself if what is in it reaches the
        agent that could act on it, so the builder is where it is spent — one
        bounded block, below the task and the project context.
        """
        lessons = (state.get("retro_lessons") or "").strip()
        if not lessons:
            return ""
        lessons = self.truncate_to_tokens(lessons, self.LESSONS_TOKEN_BUDGET)
        print("[single_agent] carrying past ticket retros into context", file=sys.stderr)
        return "\n" + lessons + "\n"

    def _max_turns(self, *, test_first: bool) -> int:
        """The turn budget for this build. Test-first work spends turns on the
        red phase before implementing, so it gets the larger budget."""
        if test_first:
            return int(getattr(self.settings, "builder_test_first_max_turns",
                               self.TEST_FIRST_MAX_TURNS))
        return int(getattr(self.settings, "builder_max_turns", self.MAX_TURNS))

    def run(self, state: TeamState) -> dict:
        task = state.get("raw_task", "")
        project_path = state.get("project_path", ".")
        run_id = state.get("run_id", 0)
        project_context = state.get("project_context") or ""

        print(f"[single_agent] Single-context implement for: {task[:80]!r}", file=sys.stderr)

        context_block = ""
        if project_context:
            oriented = self.truncate_to_tokens(project_context, self.CONTEXT_TOKEN_BUDGET)
            context_block = f"\n=== PROJECT CONTEXT (orientation only) ===\n{oriented}\n"

        test_first_block = self._test_first_block(task)
        prompt = prompts.render(
            "single-agent-implement",
            task=task,
            test_first_block=test_first_block,
            context_block=context_block,
            lessons_block=self._lessons_block(state),
        )

        cfg = self.settings.get_agent_config("single_agent")
        result = call_llm(
            cfg["provider"], cfg["model"], prompt,
            allowed_tools="Glob,Grep,Read,Edit,Write,Bash,Task,TodoWrite,WebSearch,WebFetch",
            max_turns=self._max_turns(test_first=bool(test_first_block)),
            cwd=project_path,
            on_event=_progress_reporter(run_id, "single-agent"),
        )

        if run_id:
            try:
                tracker.log_event(run_id, -1, "single_agent", result)
            except Exception as e:
                print(f"[tracker] log_event error: {e}", file=sys.stderr)

        text = (result.text or "Done").strip()
        summary = f"[single-agent] {text[:400]}"
        print(f"[single_agent] Done: {summary[:120]}", file=sys.stderr)

        return {
            **state,
            "execution_results": [summary],
            "final_summary": summary,
            # Lets the verification gate resume this session for repairs
            # instead of re-deriving the change from a cold start.
            "cli_session_id": getattr(result, "session_id", "") or "",
            # What that session has spent so far: resuming it reports a running
            # total, which the verifier turns back into each repair's own cost.
            "cli_session_cost_usd": getattr(result, "cost_usd", 0.0) or 0.0,
        }
