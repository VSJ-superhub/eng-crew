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
from ..providers import call_llm
from ..state import TeamState
from .. import tracker


class SingleAgentEngineer(BaseAgent):
    agent_type = "single_agent"

    # Budget for the project-context orientation block (kept lean on purpose).
    CONTEXT_TOKEN_BUDGET = 1200
    # Long-horizon agentic runs need room; a low cap silently truncates the
    # implementation (see claude_cli's max-turns soft-stop).
    MAX_TURNS = 60

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

        prompt = f"""You are a senior engineer implementing a focused, medium-sized change end to end in a single session. There is no separate planner, reviewer, or executor — you own the change from understanding to working code.

=== TASK ===
{task}
{context_block}
Approach:
1. Use Glob, Grep, and Read to understand the relevant code. Do not read the whole repo — target the files the task touches.
2. Form a brief internal plan, then implement it with Edit/Write. Keep the change minimal and consistent with the surrounding code's style and idioms.
3. If the project has tests for the area you changed, run them with Bash and fix failures. Do not add new test infrastructure unless the task asks for it.
4. When done, give a short summary (2-4 sentences) of what you changed and how you verified it.

If the change fans out into independent pieces (several unrelated files, a wide
search, a batch of mechanical edits), delegate those to subagents with the Task
tool and keep the integration work in your own context.

Be decisive. Prefer editing existing files over creating new ones."""

        cfg = self.settings.get_agent_config("single_agent")
        result = call_llm(
            cfg["provider"], cfg["model"], prompt,
            allowed_tools="Glob,Grep,Read,Edit,Write,Bash,Task,TodoWrite,WebSearch,WebFetch",
            max_turns=self.MAX_TURNS,
            cwd=project_path,
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
        }
