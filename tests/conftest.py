"""Shared test guards.

The suite must not reach a provider. Most tests already stub the seam they care
about, but a code path that grew a *new* model call — the ticket review after a
run lands is one — would otherwise start billing a developer's API key from
`pytest` and turn the suite flaky offline. This makes the unstubbed call an
explicit, loud failure instead. A test that wants a model call stubs
`eng_crew.agents.base.call_llm` itself, which overrides this.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_real_llm_calls(monkeypatch):
    def refuse(provider, model, prompt, **kwargs):
        raise RuntimeError(
            f"a test tried to call {provider}/{model} for real — stub "
            "eng_crew.agents.base.call_llm in the test that needs it"
        )

    monkeypatch.setattr("eng_crew.agents.base.call_llm", refuse)
