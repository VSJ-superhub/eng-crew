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
from dataclasses import replace as dc_replace

from .base import BaseAgent
from .. import prompts, tracker, verify as verify_mod
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
        # Truncation is a fact about the implementation, not about the repair's
        # account of it, so it has to survive the re-verify below.
        was_truncated = result.truncated
        print(f"[verify] {result.summary()}", file=sys.stderr)

        lock_mode = getattr(self.settings, "verification_test_lock", verify_mod.LOCK_STRICT)
        locking = lock_mode != verify_mod.LOCK_OFF and max_fixes > 0
        # Snapshot before the first repair — the last state of the tests that
        # nobody under pressure to turn the gate green has touched.
        tests_before = verify_mod.snapshot_tests(project_path) if locking else {}
        exempt_output = ""

        attempts = 0
        while not result.passed and attempts < max_fixes:
            attempts += 1
            # Each pass may implicate different tests; a file stays editable
            # if any pass was handed a failure naming it.
            exempt_output += "\n" + result.failure_report(max_chars=20000)
            print(
                f"[verify] repair pass {attempts}/{max_fixes} — {len(result.failures)} failing check(s)",
                file=sys.stderr,
            )
            self._repair(state, result, run_id, attempts)
            # Re-verify against the tree, not the repair agent's own account of it.
            # Passing agent_output="" drops the repair's narration on purpose, so
            # an established truncation is handed over explicitly — otherwise a
            # repair that accomplished nothing (or died on a provider error)
            # clears the flag and a third-finished run passes the gate.
            result = verify_mod.verify(
                project_path, agent_output="", truncated=was_truncated, timeout=timeout
            )
            if was_truncated and result.ran_any and not result.failures:
                # Real checks ran and vouch for the tree: positive evidence that
                # the repair finished the truncated work, so stop holding it.
                result = dc_replace(result, truncated=False)
                was_truncated = False
            print(f"[verify] after repair {attempts}: {result.summary()}", file=sys.stderr)

        violations: list[str] = []
        if locking and attempts:
            violations = verify_mod.lock_violations(
                tests_before,
                verify_mod.snapshot_tests(project_path),
                exempt_output,
            )
            if violations:
                print(
                    f"[verify] test lock ({lock_mode}): repair modified "
                    f"{len(violations)} test file(s) the failures never named: "
                    + ", ".join(violations),
                    file=sys.stderr,
                )
                if run_id:
                    try:
                        tracker.log_event(run_id, -1, "verify_test_lock", "\n".join(violations))
                    except Exception as exc:
                        print(f"[tracker] log_event error: {exc}", file=sys.stderr)

        lock_failed = bool(violations) and lock_mode == verify_mod.LOCK_STRICT

        # Only worth asking "do these tests prove anything?" once the suite is
        # green. On a broken tree the answer is noise, and it costs a checkout.
        red = None
        red_mode = getattr(self.settings, "verification_red_phase", verify_mod.RED_OFF)
        if red_mode != verify_mod.RED_OFF and result.passed and not lock_failed:
            try:
                red = verify_mod.verify_red(project_path, timeout=timeout)
            except Exception as exc:
                # An unprovable run is not a broken one; never crash the gate.
                print(f"[verify] red phase errored: {exc}", file=sys.stderr)
                red = None
            if red is not None:
                print(f"[verify] red phase: {red.status} — {red.output.splitlines()[0] if red.output else ''}",
                      file=sys.stderr)
                if red.status == verify_mod.FAILED and run_id:
                    try:
                        tracker.log_event(run_id, -1, "verify_red_phase", red.output)
                    except Exception as exc:
                        print(f"[tracker] log_event error: {exc}", file=sys.stderr)

        red_failed = red is not None and red.status == verify_mod.FAILED and red_mode == verify_mod.RED_STRICT
        summary = result.summary()
        if red is not None and red.status == verify_mod.FAILED:
            note = "new tests pass without the change"
            summary = f"FAILED (red phase) — {note}" if red_failed else f"{summary} [WARNING: {note}]"
        if violations:
            note = "test files rewritten during repair: " + ", ".join(violations)
            summary = (
                f"FAILED (test lock) — {note}"
                if lock_failed
                else f"{summary} [WARNING: {note}]"
            )
        execution_results = list(state.get("execution_results") or [])
        execution_results.append(f"[verify] {summary}")

        final_summary = state.get("final_summary") or ""
        if red_failed:
            final_summary = f"{final_summary}\n\n[RED] {summary}".strip()
        elif lock_failed:
            final_summary = f"{final_summary}\n\n[TEST LOCK] {summary}".strip()
        elif not result.passed:
            final_summary = f"{final_summary}\n\n[VERIFICATION FAILED] {summary}".strip()
        elif violations:
            final_summary = f"{final_summary}\n\n[TEST LOCK WARNING] {summary}".strip()
        elif result.unverified:
            final_summary = f"{final_summary}\n\n[UNVERIFIED] {summary}".strip()

        return {
            **state,
            "execution_results": execution_results,
            "final_summary": final_summary,
            "verification_passed": result.passed and not lock_failed and not red_failed,
            "verification_summary": summary,
            "verification_unverified": result.unverified and not lock_failed and not red_failed,
            "verification_test_lock_violations": violations or None,
            "verification_red_phase": red.status if red is not None else None,
            "verify_fix_count": attempts,
        }

    # ------------------------------------------------------------------

    def _agent_output(self, state: TeamState) -> str:
        """Everything the execution tiers reported, for truncation detection."""
        parts = [state.get("final_summary") or ""]
        parts.extend(state.get("execution_results") or [])
        return "\n".join(p for p in parts if p)

    def _progress(self, run_id: int, label: str):
        from ..providers.claude_cli import summarize_event

        def report(evt: dict) -> None:
            line = summarize_event(evt)
            if line:
                tracker.update_run_progress(run_id, -1, f"{label}: {line}")

        return report

    def _repair(self, state: TeamState, result, run_id: int, attempt: int) -> None:
        task = state.get("raw_task", "")
        project_path = state.get("project_path", ".")

        session_id = state.get("cli_session_id") or ""

        if session_id:
            # Resuming: the agent still has the change it just made in context,
            # so restating the task would only add noise.
            prompt = prompts.render(
                "repair-verification-failure-resumed",
                failures=result.failure_report(),
            )
        else:
            prompt = prompts.render(
                "repair-verification-failure",
                task=task,
                failures=result.failure_report(),
            )

        cfg = self.settings.get_agent_config("single_agent")
        if session_id:
            print(f"[verify] resuming session {session_id[:8]}… for repair", file=sys.stderr)
        try:
            llm_result = call_llm(
                cfg["provider"], cfg["model"], prompt,
                allowed_tools=self.REPAIR_TOOLS,
                max_turns=self.REPAIR_MAX_TURNS,
                cwd=project_path,
                resume_session=session_id or None,
                on_event=self._progress(run_id, f"repair {attempt}"),
            )
        except Exception as exc:  # a failed repair must not crash the gate
            print(f"[verify] repair call failed: {exc}", file=sys.stderr)
            return

        if run_id:
            try:
                tracker.log_event(run_id, -1, f"verify_repair_{attempt}", llm_result)
            except Exception as exc:
                print(f"[tracker] log_event error: {exc}", file=sys.stderr)
