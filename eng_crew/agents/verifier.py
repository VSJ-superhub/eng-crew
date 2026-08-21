"""Verification gate — the last node before a run is allowed to be a success.

Every execution tier asks the model to run tests. This node checks, using
``eng_crew.verify`` (no LLM). When checks fail it spends a bounded number of
repair passes handing the real failure output back to a CLI agent, then
re-runs. If the tree is still broken the run is marked failed rather than
completed — a broken tree recorded as success is the failure mode this exists
to prevent.
"""
from __future__ import annotations

import sys

from .base import BaseAgent
from .. import tracker, verify as verify_mod
from ..providers import call_llm
from ..state import TeamState


class VerifierAgent(BaseAgent):
    agent_type = "verifier"

    REPAIR_MAX_TURNS = 40
    REPAIR_TOOLS = "Glob,Grep,Read,Edit,Write,Bash"

    def run(self, state: TeamState) -> dict:
        project_path = state.get("project_path", ".")
        run_id = state.get("run_id", 0) or 0

        enabled = getattr(self.settings, "verification_enabled", True)
        if not enabled:
            print("[verify] disabled by settings — skipping gate", file=sys.stderr)
            return {**state, "verification_passed": True, "verification_summary": "skipped (disabled)"}

        timeout = getattr(self.settings, "verification_timeout", 300)
        max_fixes = getattr(self.settings, "verification_max_fix_attempts", 1)

        agent_output = self._agent_output(state)
        result = verify_mod.verify(project_path, agent_output=agent_output, timeout=timeout)
        print(f"[verify] {result.summary()}", file=sys.stderr)

        attempts = 0
        while not result.passed and attempts < max_fixes:
            attempts += 1
            print(
                f"[verify] repair pass {attempts}/{max_fixes} — {len(result.failures)} failing check(s)",
                file=sys.stderr,
            )
            self._repair(state, result, run_id, attempts)
            # Re-verify against the tree, not the repair agent's own account of it.
            result = verify_mod.verify(project_path, agent_output="", timeout=timeout)
            print(f"[verify] after repair {attempts}: {result.summary()}", file=sys.stderr)

        summary = result.summary()
        execution_results = list(state.get("execution_results") or [])
        execution_results.append(f"[verify] {summary}")

        final_summary = state.get("final_summary") or ""
        if not result.passed:
            final_summary = f"{final_summary}\n\n[VERIFICATION FAILED] {summary}".strip()
        elif result.unverified:
            final_summary = f"{final_summary}\n\n[UNVERIFIED] {summary}".strip()

        return {
            **state,
            "execution_results": execution_results,
            "final_summary": final_summary,
            "verification_passed": result.passed,
            "verification_summary": summary,
            "verification_unverified": result.unverified,
            "verify_fix_count": attempts,
        }

    # ------------------------------------------------------------------

    def _agent_output(self, state: TeamState) -> str:
        """Everything the execution tiers reported, for truncation detection."""
        parts = [state.get("final_summary") or ""]
        parts.extend(state.get("execution_results") or [])
        return "\n".join(p for p in parts if p)

    def _repair(self, state: TeamState, result, run_id: int, attempt: int) -> None:
        task = state.get("raw_task", "")
        project_path = state.get("project_path", ".")

        prompt = f"""The change below was implemented, but verification failed. Fix it.

=== ORIGINAL TASK ===
{task}

=== VERIFICATION FAILURES ===
{result.failure_report()}

Fix the cause of these failures. Read the relevant code first — the failure may be in the
implementation or in a test that the change made stale, so decide which is actually wrong
rather than forcing either to match the other. Do not delete, skip, or weaken a test to make
it pass. Re-run the failing command yourself to confirm the fix before you finish."""

        cfg = self.settings.get_agent_config("single_agent")
        try:
            llm_result = call_llm(
                cfg["provider"], cfg["model"], prompt,
                allowed_tools=self.REPAIR_TOOLS,
                max_turns=self.REPAIR_MAX_TURNS,
                cwd=project_path,
            )
        except Exception as exc:  # a failed repair must not crash the gate
            print(f"[verify] repair call failed: {exc}", file=sys.stderr)
            return

        if run_id:
            try:
                tracker.log_event(run_id, -1, f"verify_repair_{attempt}", llm_result)
            except Exception as exc:
                print(f"[tracker] log_event error: {exc}", file=sys.stderr)
