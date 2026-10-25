"""The Claude CLI's error results are errors, not answers.

A usage limit arrives as an ordinary `result` event whose text is the error and
whose `is_error` is set. Passed on as text it became a builder's "summary" and a
reviewer's "verdict"; the provider raises it instead.
"""
from __future__ import annotations

import pytest

from eng_crew.providers import claude_cli


def _provider(monkeypatch, got: dict):
    provider = claude_cli.ClaudeCLIProvider()
    calls = []
    monkeypatch.setattr(provider, "_build_cmd", lambda *a, **k: ["claude"])
    monkeypatch.setattr(provider, "_attempt", lambda *a, **k: calls.append(1) or dict(got))
    return provider, calls


BASE = {"text": "", "session_id": "s", "input_tokens": 0, "output_tokens": 0,
        "cost_usd": 0.0, "truncated": False, "got_result": True, "is_error": False}


def test_an_error_result_is_raised_and_not_retried(monkeypatch):
    provider, calls = _provider(monkeypatch, {
        **BASE, "text": "You've hit your session limit · resets 12:50pm", "is_error": True,
    })
    with pytest.raises(claude_cli.CLIErrorResult, match="session limit"):
        provider.call("claude-opus-5", "do the thing")
    assert len(calls) == 1, "a usage limit is not worth retrying seconds later"


def test_a_normal_result_is_returned_as_text(monkeypatch):
    provider, _ = _provider(monkeypatch, {**BASE, "text": "APPROVE"})
    assert provider.call("claude-opus-5", "review this").text == "APPROVE"


def test_a_session_stopped_by_its_spending_cap_is_partial_work_not_an_error(monkeypatch):
    """`--max-budget-usd` ends the session with its own subtype. That is the
    limit eng-crew asked for: keep the partial work, marked, and do not retry —
    a retry would spend the cap all over again."""
    provider = claude_cli.ClaudeCLIProvider()
    stream = [{"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
               "result": "half done", "total_cost_usd": 5.17, "session_id": "s"}]
    monkeypatch.setattr(provider, "_stream", lambda *a, **k: iter(stream))
    attempts = []
    real = provider._attempt
    monkeypatch.setattr(provider, "_attempt", lambda *a, **k: attempts.append(1) or real(*a, **k))

    result = provider.call("claude-opus-5", "repair it", max_budget_usd=5.17)

    assert len(attempts) == 1
    assert result.text.startswith(claude_cli.TRUNCATION_PREFIX)
    assert "half done" in result.text
    assert result.cost_usd == pytest.approx(5.17)
