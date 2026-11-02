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


@pytest.fixture(autouse=True)
def no_real_discord_posts(monkeypatch):
    """The developer's `.env` webhook must never receive a test's ticket.

    Any test that moves a ticket to `done` would otherwise push to the real
    channel. The configured webhook is blanked, and the post itself refused in
    case a test builds its own settings from `.env`. A test that wants a push
    sets the webhook and stubs `eng_crew.notify.httpx.post`, which overrides
    this.
    """
    from eng_crew import config, notify

    monkeypatch.setattr(config.settings, "discord_webhook_url", "")
    monkeypatch.setattr(config.settings, "discord_notify_user_id", "")

    def refuse(url, **kwargs):
        raise RuntimeError("a test tried to post to Discord for real — stub "
                           "eng_crew.notify.httpx.post in the test that needs it")

    monkeypatch.setattr(notify.httpx, "post", refuse)
