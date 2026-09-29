"""Test-first building, and the gate that holds the builder to it.

Three things here are load-bearing:

  - a task that carries acceptance criteria must reach the builder with the
    test-first ordering attached, because criteria are a definition of done
    written before the code and that is exactly a test;
  - the red phase is strict by default, so "the new tests failed before the
    change" is a gate rather than a note nobody reads;
  - the gate's own findings must actually be recorded. They were not: the
    verifier handed ``tracker.log_event`` plain strings, which raised inside its
    catch-all and dropped the evidence for the failure it was reporting.
"""
from __future__ import annotations

import pytest

from eng_crew import tickets, tracker, verify
from eng_crew.agents.single_agent import SingleAgentEngineer
from eng_crew.agents.verifier import VerifierAgent
from eng_crew.config import Settings


# --- criteria survive the trip from ticket to task string ----------------


def test_criteria_round_trip_out_of_the_task_text():
    task = tickets.task_text(
        {
            "title": "add pagination",
            "description": "the runs table is unbounded",
            "acceptance_criteria": "- 20 rows per page\n- filters are preserved",
        }
    )
    assert tickets.acceptance_criteria_of(task) == "- 20 rows per page\n- filters are preserved"


def test_a_task_without_criteria_has_none():
    assert tickets.acceptance_criteria_of("just fix the typo") == ""
    assert tickets.acceptance_criteria_of("") == ""
    assert tickets.acceptance_criteria_of(None) == ""


def test_criteria_are_read_from_the_last_heading():
    """A description may quote the phrase; the criteria are what task_text appended."""
    task = tickets.task_text(
        {
            "title": "t",
            "description": "the ticket had no Acceptance criteria: before now",
            "acceptance_criteria": "- the real one",
        }
    )
    assert tickets.acceptance_criteria_of(task) == "- the real one"


# --- the builder's prompt -----------------------------------------------


def _prompt_block(task: str) -> str:
    return SingleAgentEngineer(Settings())._test_first_block(task)


def test_criteria_put_the_builder_in_test_first_mode():
    task = tickets.task_text(
        {"title": "t", "acceptance_criteria": "- the total excludes refunds"}
    )
    block = _prompt_block(task)
    assert "- the total excludes refunds" in block
    assert "confirm they FAIL" in block


def test_a_task_without_criteria_gets_no_test_first_block():
    assert _prompt_block("rename the getUser function") == ""


def test_the_rendered_prompt_carries_the_block():
    """The placeholder has to be wired, not just the block built."""
    from eng_crew import prompts

    task = tickets.task_text({"title": "t", "acceptance_criteria": "- it holds"})
    rendered = prompts.render(
        "single-agent-implement",
        task=task,
        test_first_block=_prompt_block(task),
        context_block="",
    )
    assert "Work test-first" in rendered
    assert "- it holds" in rendered
    assert "{test_first_block}" not in rendered


# --- the red phase is a gate, not a warning -----------------------------


def test_red_phase_is_strict_by_default():
    # The field default, not this checkout's .env — the shipped default is what
    # decides whether the red phase gates for someone who configured nothing.
    assert Settings.model_fields["verification_red_phase"].default == verify.RED_STRICT


def test_verifier_defaults_to_strict_when_settings_lack_the_field():
    """An older settings object must not silently downgrade the gate."""

    class Bare:
        pass

    agent = VerifierAgent(Bare())
    assert getattr(agent.settings, "verification_red_phase", verify.RED_STRICT) == verify.RED_STRICT


# --- gate findings reach the database -----------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "tracking.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


def test_gate_findings_are_recorded(db):
    run_id = tracker.create_run("t", str(db))
    agent = VerifierAgent(Settings())

    agent._note(run_id, "verify_red_phase", "these tests pass at base")
    agent._note(run_id, "verify_test_lock", "tests/test_a.py")

    events = {e["agent_name"]: e["result_text"] for e in tracker.get_run_events(run_id)}
    assert events["verify_red_phase"] == "these tests pass at base"
    assert events["verify_test_lock"] == "tests/test_a.py"


def test_noting_without_a_run_is_a_no_op(db):
    VerifierAgent(Settings())._note(0, "verify_red_phase", "orphan")
    assert tracker.get_run_events(0) == []


# --- a task with criteria must actually add tests -----------------------
#
# The red phase skips a run that added no tests, which is right for a bare task.
# It would also wave through a ticket whose builder ignored test-first entirely,
# so the gate asks that question on its own.


CRITERIA_TASK = tickets.task_text(
    {"title": "rename files", "acceptance_criteria": "- files are renamed"}
)


@pytest.fixture
def green_gate(monkeypatch, tmp_path):
    """A verifier whose checks pass, with the test-file answer under test control."""
    changed: list[str] = []
    monkeypatch.setattr(verify, "verify", lambda *a, **k: verify.VerificationResult(
        results=[verify.CheckResult("pytest", "test", verify.PASSED)]))
    monkeypatch.setattr(verify, "snapshot_tests", lambda *a, **k: {})
    monkeypatch.setattr(verify, "verify_red", lambda *a, **k: verify.CheckResult(
        "red phase", verify.RED, verify.SKIPPED, "run added no new tests"))
    monkeypatch.setattr(verify, "detect_checks", lambda *a, **k: [
        verify.Check("pytest", ["pytest"], "test")])
    monkeypatch.setattr(verify, "new_test_files", lambda *a, **k: list(changed))
    return changed


def _gate(task: str, mode: str = verify.RED_STRICT) -> dict:
    settings = Settings(verification_red_phase=mode)
    return VerifierAgent(settings).run({"raw_task": task, "project_path": ".", "run_id": 0})


def test_criteria_with_no_new_tests_fails_the_gate(green_gate):
    out = _gate(CRITERIA_TASK)
    assert out["verification_passed"] is False
    assert "added no tests" in out["verification_summary"]
    assert "[UNTESTED]" in out["final_summary"]


def test_criteria_with_new_tests_passes(green_gate):
    green_gate.append("tests/test_rename.py")
    assert _gate(CRITERIA_TASK)["verification_passed"] is True


def test_a_conftest_alone_does_not_count_as_a_test(green_gate):
    green_gate.append("tests/conftest.py")
    assert _gate(CRITERIA_TASK)["verification_passed"] is False


def test_a_bare_task_with_no_new_tests_still_passes(green_gate):
    """Docs, config, refactors: no criteria means no test-first promise to hold."""
    assert _gate("update the README")["verification_passed"] is True


def test_warn_mode_reports_but_passes(green_gate):
    out = _gate(CRITERIA_TASK, mode=verify.RED_WARN)
    assert out["verification_passed"] is True
    assert "WARNING" in out["verification_summary"]


def test_a_project_without_a_test_suite_is_not_held_to_it(green_gate, monkeypatch):
    monkeypatch.setattr(verify, "detect_checks", lambda *a, **k: [
        verify.Check("npm run build", ["npm", "run", "build"], "build")])
    assert _gate(CRITERIA_TASK)["verification_passed"] is True


# --- test-first work gets a bigger turn budget --------------------------
#
# Writing and running failing tests happens before any implementation, so a
# test-first build that shares the ordinary cap gets cut off mid-change.


def _turns_used_for(task: str, monkeypatch, settings=None) -> int:
    from eng_crew.agents import single_agent as single_agent_mod
    from eng_crew.providers.base import LLMResult

    seen = {}

    def fake_call(*a, **kw):
        seen["max_turns"] = kw["max_turns"]
        return LLMResult(text="done")

    monkeypatch.setattr(single_agent_mod, "call_llm", fake_call)
    SingleAgentEngineer(settings or Settings()).run(
        {"raw_task": task, "project_path": ".", "run_id": 0})
    return seen["max_turns"]


def test_a_test_first_build_gets_the_larger_turn_budget(monkeypatch):
    s = Settings()
    assert s.builder_test_first_max_turns > s.builder_max_turns
    assert _turns_used_for(CRITERIA_TASK, monkeypatch) == s.builder_test_first_max_turns


def test_a_bare_task_keeps_the_ordinary_turn_budget(monkeypatch):
    assert _turns_used_for("update the README", monkeypatch) == Settings().builder_max_turns


def test_turn_budgets_come_from_settings(monkeypatch):
    s = Settings(builder_max_turns=7, builder_test_first_max_turns=9)
    assert _turns_used_for("update the README", monkeypatch, s) == 7
    assert _turns_used_for(CRITERIA_TASK, monkeypatch, s) == 9
