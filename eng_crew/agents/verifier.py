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
from .. import budget, prompts, tickets, tracker, verify as verify_mod
from ..providers import call_llm
from ..providers.base import LLMResult
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
        repair_account = ""
        while not result.passed and attempts < max_fixes:
            # A repair pass is a new paid step. The build that just overran its
            # ticket's budget was allowed to finish — this is where the spending
            # stops, before another agent session starts. A run with no ticket
            # has no budget and is never held here.
            if not budget.allows_for_run(run_id, "a repair pass", self.settings):
                print("[verify] repair skipped — the ticket is over budget",
                      file=sys.stderr)
                break
            attempts += 1
            # Each pass may implicate different tests; a file stays editable
            # if any pass was handed a failure naming it.
            exempt_output += "\n" + result.failure_report(max_chars=20000)
            print(
                f"[verify] repair pass {attempts}/{max_fixes} — {len(result.failures)} failing check(s)",
                file=sys.stderr,
            )
            # And the pass itself may only spend what the ticket has left: one
            # repair session must not carry a ticket from under its budget to
            # far over it. None for a run with no ticket.
            ticket_budget = budget.status_for_run(run_id, self.settings) if run_id else None
            cap = ticket_budget["remaining_usd"] if ticket_budget else None
            repair_account = (
                self._repair(state, result, run_id, attempts, max_budget_usd=cap)
                or repair_account
            )
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
                self._note(run_id, "verify_test_lock", "\n".join(violations))

        lock_failed = bool(violations) and lock_mode == verify_mod.LOCK_STRICT

        # Only worth asking "do these tests prove anything?" once the suite is
        # green. On a broken tree the answer is noise, and it costs a checkout.
        red = None
        red_mode = getattr(self.settings, "verification_red_phase", verify_mod.RED_STRICT)
        if red_mode != verify_mod.RED_OFF and result.passed and not lock_failed:
            try:
                red = verify_mod.verify_red(
                    project_path, base=self._base(state), timeout=timeout
                )
            except Exception as exc:
                # An unprovable run is not a broken one; never crash the gate.
                print(f"[verify] red phase errored: {exc}", file=sys.stderr)
                red = None
            if red is not None:
                print(f"[verify] red phase: {red.status} — {red.output.splitlines()[0] if red.output else ''}",
                      file=sys.stderr)
                if red.status == verify_mod.FAILED:
                    self._note(run_id, "verify_red_phase", red.output)

        red_failed = red is not None and red.status == verify_mod.FAILED and red_mode == verify_mod.RED_STRICT

        # The red phase skips a run that added no tests, which is right for a
        # bare task — but a task with acceptance criteria was told to encode
        # them as tests first. Adding none means test-first never happened.
        untested = False
        if red_mode != verify_mod.RED_OFF and result.passed and not lock_failed:
            untested = self._criteria_untested(state, project_path)
            if untested:
                print("[verify] acceptance criteria present but the run added no tests",
                      file=sys.stderr)
                self._note(run_id, "verify_untested_criteria",
                           tickets.acceptance_criteria_of(state.get("raw_task", "")))
        untested_failed = untested and red_mode == verify_mod.RED_STRICT

        summary = result.summary()
        if red is not None and red.status == verify_mod.FAILED:
            note = "new tests pass without the change"
            summary = f"FAILED (red phase) — {note}" if red_failed else f"{summary} [WARNING: {note}]"
        if untested:
            note = "the task has acceptance criteria but the run added no tests"
            summary = f"FAILED (untested) — {note}" if untested_failed else f"{summary} [WARNING: {note}]"
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
        if verify_mod.TRUNCATION_MARKER in final_summary and repair_account:
            # A builder cut off by its turn limit leaves no account of its work,
            # only the truncation notice. The repair pass that finished the job
            # wrote one, so that is what the run's summary should say.
            final_summary = (
                f"{repair_account}\n\n(The implementation pass hit its turn limit; "
                "this is the repair pass's account.)"
            )
        if red_failed:
            final_summary = f"{final_summary}\n\n[RED] {summary}".strip()
        elif untested_failed:
            final_summary = f"{final_summary}\n\n[UNTESTED] {summary}".strip()
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
            "verification_passed": (
                result.passed and not lock_failed and not red_failed and not untested_failed
            ),
            "verification_summary": summary,
            "verification_unverified": (
                result.unverified and not lock_failed and not red_failed and not untested_failed
            ),
            "verification_test_lock_violations": violations or None,
            "verification_red_phase": red.status if red is not None else None,
            "verify_fix_count": attempts,
        }

    # ------------------------------------------------------------------

    def _criteria_untested(self, state: TeamState, project_path: str) -> bool:
        """Did a task with acceptance criteria finish without adding any tests?

        Only asked of projects that have a test suite: with nowhere to put a
        test, "no tests" is not the builder skipping the step.
        """
        if not tickets.acceptance_criteria_of(state.get("raw_task", "")):
            return False
        try:
            if not any(c.kind == "test" for c in verify_mod.detect_checks(project_path)):
                return False
            changed = verify_mod.new_test_files(project_path, self._base(state))
        except Exception as exc:  # an unanswerable question never fails the gate
            print(f"[verify] untested-criteria check errored: {exc}", file=sys.stderr)
            return False
        return not [p for p in changed if p.rsplit("/", 1)[-1] != "conftest.py"]

    def _base(self, state: TeamState) -> str:
        """The commit this run started from; HEAD for runs that never recorded one.

        Diffing against HEAD only works while the builder leaves its output
        uncommitted. One that commits makes HEAD equal its own work, and every
        test it wrote looks pre-existing.
        """
        return state.get("base_commit") or "HEAD"

    def _note(self, run_id: int, name: str, text: str) -> None:
        """Record a gate finding as a zero-cost event on the run's timeline.

        ``tracker.log_event`` takes an LLMResult; handing it a bare string made
        it raise on ``.text`` inside its own catch-all, so the lock and red-phase
        findings — the evidence for failing the run — never reached the database.
        """
        if not run_id:
            return
        tracker.log_event(run_id, -1, name, LLMResult(text=text))

    def _agent_output(self, state: TeamState) -> str:
        """Everything the execution tiers reported, for truncation detection."""
        parts = [state.get("final_summary") or ""]
        parts.extend(state.get("execution_results") or [])
        return "\n".join(p for p in parts if p)

    def _progress(self, run_id: int, label: str):
        """Progress rows and the run's tool-call trace, for a repair pass."""
        from ..tracing import run_event_handler

        return run_event_handler(run_id, label)

    def _repair(self, state: TeamState, result, run_id: int, attempt: int,
                *, max_budget_usd: float | None = None) -> str:
        """Run one repair pass. Returns the repair agent's own account, or "".

        ``max_budget_usd`` caps what the session may spend — the ticket's
        remaining budget. A provider that cannot cap a session ignores it.
        """
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
                max_budget_usd=max_budget_usd,
            )
        except Exception as exc:  # a failed repair must not crash the gate
            print(f"[verify] repair call failed: {exc}", file=sys.stderr)
            return ""

        if run_id:
            try:
                tracker.log_event(run_id, -1, f"verify_repair_{attempt}", llm_result)
            except Exception as exc:
                print(f"[tracker] log_event error: {exc}", file=sys.stderr)
        text = (llm_result.text or "").strip()
        # A repair cut off by its own turn limit has no account worth keeping.
        return "" if verify_mod.TRUNCATION_MARKER in text else text
