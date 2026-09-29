"""What a run's summary says when the builder was cut off and a repair finished.

A builder that hits its turn limit leaves only the truncation notice as its
summary. When the repair pass completes the work, its account is the only
description of what the run did, so the summary must carry it rather than a
bare "[TRUNCATED]" line.
"""
from __future__ import annotations

import pytest

from eng_crew import verify
from eng_crew.agents import verifier as verifier_mod
from eng_crew.agents.verifier import VerifierAgent
from eng_crew.config import Settings
from eng_crew.providers.base import LLMResult

TRUNCATED = "[single-agent] [TRUNCATED: max turns reached — the implementation may be incomplete]"


@pytest.fixture
def truncated_then_green(monkeypatch):
    """First verify sees truncation; after the repair, the checks pass."""
    calls = {"n": 0}

    def fake_verify(*a, **k):
        calls["n"] += 1
        green = verify.CheckResult("pytest", "test", verify.PASSED)
        return verify.VerificationResult(results=[green], truncated=calls["n"] == 1)

    monkeypatch.setattr(verify, "verify", fake_verify)
    monkeypatch.setattr(verify, "snapshot_tests", lambda *a, **k: {})
    monkeypatch.setattr(verify, "verify_red", lambda *a, **k: None)
    replies: list[str] = []
    monkeypatch.setattr(
        verifier_mod, "call_llm",
        lambda *a, **k: LLMResult(text=replies.pop(0) if replies else ""),
    )
    return replies


def _gate() -> dict:
    return VerifierAgent(Settings()).run(
        {"raw_task": "do it", "project_path": ".", "run_id": 0, "final_summary": TRUNCATED}
    )


def test_the_repairs_account_replaces_the_truncation_notice(truncated_then_green):
    truncated_then_green.append("Finished the Discord docstring; suite green.")
    out = _gate()

    assert out["verification_passed"] is True
    assert out["final_summary"].startswith("Finished the Discord docstring; suite green.")
    assert verify.TRUNCATION_MARKER not in out["final_summary"]
    assert "turn limit" in out["final_summary"]


def test_a_silent_repair_leaves_the_truncation_notice(truncated_then_green):
    out = _gate()
    assert out["final_summary"].startswith(TRUNCATED)


def test_a_truncated_repair_is_not_used_as_the_account(truncated_then_green):
    truncated_then_green.append("[TRUNCATED: max turns reached] partway")
    out = _gate()
    assert out["final_summary"].startswith(TRUNCATED)
