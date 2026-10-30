"""The test lock, and what the reviewer hears, when the builder was cut off.

Runs 100326 and 100327 hit the builder's turn limit with their tests
half-written. The repair pass finished the build and the suite went green, but
the lock had snapshotted the builder's own unfinished tests and rejected both
runs for touching them. The pass that finishes a truncated build is part of the
build, so it is not locked; the tests it changed are handed to the ticket
reviewer instead, and the lock still holds every pass after it.
"""
from __future__ import annotations

import pytest

from eng_crew import review, tracker, verify
from eng_crew.agents import verifier as verifier_mod
from eng_crew.agents.verifier import (
    CONTINUATION_TESTS_EVENT,
    TEST_LOCK_EVENT,
    VerifierAgent,
)
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult

TRUNCATED = "[single-agent] [TRUNCATED: max turns reached — the implementation may be incomplete]"

RED = verify.CheckResult("pytest", "test", verify.FAILED, output="1 failed")
GREEN = verify.CheckResult("pytest", "test", verify.PASSED)


@pytest.fixture
def gate(monkeypatch):
    """A gate whose verify results and test snapshots are scripted.

    ``verdicts`` is the sequence of verify results (True = green); ``snapshots``
    is the sequence of test-tree snapshots, one taken before the first pass and
    one after each pass that is snapshotted. Notes are captured, not stored.
    """
    script = {"verdicts": [], "snapshots": [], "truncated_first": True}
    notes: dict[str, str] = {}
    calls = {"n": 0}

    def fake_verify(*a, **k):
        calls["n"] += 1
        green = script["verdicts"].pop(0)
        truncated = (calls["n"] == 1 and script["truncated_first"]) or k.get("truncated", False)
        return verify.VerificationResult(results=[GREEN if green else RED], truncated=truncated)

    monkeypatch.setattr(verify, "verify", fake_verify)
    monkeypatch.setattr(verify, "snapshot_tests", lambda *a, **k: script["snapshots"].pop(0))
    monkeypatch.setattr(verify, "verify_red", lambda *a, **k: None)
    monkeypatch.setattr(verifier_mod, "call_llm", lambda *a, **k: LLMResult(text="done"))
    monkeypatch.setattr(VerifierAgent, "_note", lambda self, run_id, name, text: notes.__setitem__(name, text))

    def run(**settings):
        summary = TRUNCATED if script["truncated_first"] else "built it"
        return VerifierAgent(Settings(**settings)).run(
            {"raw_task": "do it", "project_path": ".", "run_id": 0, "final_summary": summary}
        )

    return script, notes, run


def test_the_pass_that_finishes_a_truncated_build_may_write_its_tests(gate):
    script, notes, run = gate
    script["verdicts"] = [True, True]
    script["snapshots"] = [
        {"tests/test_offer_limits.py": "half-written"},
        {"tests/test_offer_limits.py": "finished", "tests/test_new.py": "added"},
    ]

    out = run()

    assert out["verification_passed"] is True, out["verification_summary"]
    assert out["verification_test_lock_violations"] is None
    assert notes[CONTINUATION_TESTS_EVENT] == "tests/test_new.py\ntests/test_offer_limits.py"
    assert TEST_LOCK_EVENT not in notes


def test_a_continuation_that_touched_no_tests_leaves_no_note(gate):
    script, notes, run = gate
    script["verdicts"] = [True, True]
    script["snapshots"] = [{"tests/test_a.py": "a"}, {"tests/test_a.py": "a"}]

    assert run()["verification_passed"] is True
    assert notes == {}


def test_a_repair_after_the_continuation_is_still_locked(gate):
    script, notes, run = gate
    # Truncated; the continuation leaves a failing test; the real repair then
    # rewrites a test the failure never named.
    script["verdicts"] = [True, False, True]
    script["snapshots"] = [
        {"tests/test_a.py": "a"},
        {"tests/test_a.py": "a2"},
        {"tests/test_a.py": "weakened"},
    ]

    out = run(verification_max_fix_attempts=2)

    assert out["verification_passed"] is False
    assert out["verification_test_lock_violations"] == ["tests/test_a.py"]
    assert "test lock" in out["verification_summary"]


def test_a_repair_of_a_finished_build_is_still_locked(gate):
    script, notes, run = gate
    script["truncated_first"] = False
    script["verdicts"] = [False, True]
    script["snapshots"] = [{"tests/test_a.py": "a"}, {"tests/test_a.py": "weakened"}]

    out = run()

    assert out["verification_passed"] is False
    assert out["verification_test_lock_violations"] == ["tests/test_a.py"]
    assert CONTINUATION_TESTS_EVENT not in notes


# --- the reviewer hand-off ----------------------------------------------


def _events(monkeypatch, events: list[dict]) -> None:
    monkeypatch.setattr(tracker, "get_run_events", lambda run_id: events)


def test_the_reviewer_is_told_which_tests_the_continuation_changed(monkeypatch):
    _events(monkeypatch, [
        {"agent_name": CONTINUATION_TESTS_EVENT,
         "result_text": "api/test_offer_limits.py\napps/web/tests/fixtures.ts"},
    ])

    note = review._verification_note({"id": 7, "status": review.VERIFIED})

    assert note.startswith("The run passed the project's tests and build.")
    assert "turn limit" in note
    assert "- api/test_offer_limits.py" in note and "- apps/web/tests/fixtures.ts" in note
    assert "loosened" in note, "the reviewer is not told what to check them for"


def test_the_reviewer_is_told_about_tests_a_warn_mode_lock_let_through(monkeypatch):
    _events(monkeypatch, [{"agent_name": TEST_LOCK_EVENT, "result_text": "tests/test_a.py"}])

    note = review._verification_note({"id": 7, "status": review.VERIFIED})

    assert "never named" in note and "- tests/test_a.py" in note


def test_a_run_with_no_test_changes_reads_as_before(monkeypatch):
    _events(monkeypatch, [{"agent_name": "single_agent", "result_text": "built it"}])

    assert review._verification_note({"id": 7, "status": review.VERIFIED}) == (
        "The run passed the project's tests and build."
    )
    assert review._verification_note(None) == "The run finished as unknown."


def test_the_handoff_reaches_the_reviewers_prompt(monkeypatch):
    _events(monkeypatch, [
        {"agent_name": CONTINUATION_TESTS_EVENT, "result_text": "api/test_offer_limits.py"},
    ])
    seen = {}

    class FakeAgent:
        def __init__(self, settings):
            pass

        def call(self, prompt, **kwargs):
            seen["prompt"] = prompt
            return LLMResult(text="APPROVE — all met")

    monkeypatch.setattr("eng_crew.agents.base.BaseAgent", FakeAgent)
    review._ask(
        {"title": "t", "description": "d", "acceptance_criteria": "- c"},
        {"id": 7, "status": review.VERIFIED},
        "diff --git a/x b/x\n", run_id=7, settings=Settings(),
        reviewer={"provider": "claude_cli", "model": "m", "sandbox": False},
    )

    assert "- api/test_offer_limits.py" in seen["prompt"]
