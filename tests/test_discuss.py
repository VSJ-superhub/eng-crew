"""The discussion agent, and the routing that decides talk vs build.

The risk here is a false build: someone asks a question and gets a confirm
dialog offering to change their code. So the routing tests lean hard on
questions never being treated as work orders, and the agent tests pin that it
has no way to write anything even if it wanted to.
"""
from __future__ import annotations

import pytest

from eng_crew import discuss
from eng_crew.providers.base import LLMResult


# --- talk or build? ------------------------------------------------------


QUESTIONS = [
    "how does WAL mode actually work in sqlite?",
    "how does WAL mode actually work in sqlite",
    "why is my dashboard slow",
    "what's the difference between a worktree and a branch?",
    "should I use redis for this",
    "explain how langgraph checkpointing works",
    "is postgres overkill for 200 rows",
    "can you compare celery and rq",
    "tell me about SSE",
    "any idea why sqlite locks up",
    "does resolvemind use websockets?",
    "thoughts on switching to uv",
]

DIRECTIVES = [
    "resolvemind add a logout button",
    "add dark mode",
    "fix the login bug",
    "build me a CLI that renames photos",
    "refactor the tracker to use a connection pool",
    "write tests for the intake router",
    "migrate the database to postgres",
]


@pytest.mark.parametrize("text", QUESTIONS)
def test_questions_are_for_discussion(text):
    assert discuss.is_question(text) is True, f"would have offered to build: {text!r}"


@pytest.mark.parametrize("text", DIRECTIVES)
def test_directives_are_not_questions(text):
    assert discuss.is_question(text) is False, f"would refuse to build: {text!r}"


def test_empty_text_is_not_a_question():
    assert discuss.is_question("") is False
    assert discuss.is_question("   ") is False


def test_a_question_mark_wins_over_a_directive_opening():
    # "add dark mode?" is someone floating an idea, not ordering one.
    assert discuss.is_question("add dark mode?") is True


# --- history -------------------------------------------------------------


def test_history_is_trimmed_to_the_most_recent_turns():
    history = [{"role": "user", "content": str(i)} for i in range(50)]
    trimmed = discuss.trim_history(history, max_turns=6)
    assert len(trimmed) == 6
    assert trimmed[-1]["content"] == "49", "kept the oldest turns instead of the newest"


def test_short_history_is_untouched():
    history = [{"role": "user", "content": "a"}]
    assert discuss.trim_history(history) == history


def test_empty_history_is_fine():
    assert discuss.trim_history([]) == []
    assert discuss.trim_history(None or []) == []


# --- the agent -----------------------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    class Fake:
        def __init__(self):
            self.reply = "here's how it works"
            self.prompt = ""
            self.kwargs: dict = {}

        def __call__(self, provider, model, prompt, **kwargs):
            self.prompt = prompt
            self.kwargs = {"provider": provider, "model": model, **kwargs}
            return LLMResult(text=self.reply, provider=provider, model=model)

    fake = Fake()
    monkeypatch.setattr(discuss, "call_llm", fake)
    return fake


def test_discuss_returns_the_reply(fake_llm):
    fake_llm.reply = "WAL keeps a write-ahead log instead of rolling back."
    assert discuss.discuss("how does WAL work?").reply.startswith("WAL keeps")


def test_discuss_never_returns_an_empty_reply(fake_llm):
    fake_llm.reply = ""
    assert discuss.discuss("hi").reply == "(no response)"


def test_the_agent_cannot_write_anything(fake_llm):
    """The whole point: this is a thinking partner, not a builder."""
    discuss.discuss("how does this work?")
    tools = set(fake_llm.kwargs["allowed_tools"].split(","))
    assert tools == {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
    for forbidden in ("Edit", "Write", "Bash", "Task"):
        assert forbidden not in tools


def test_it_can_search_the_web(fake_llm):
    # "Learn about things" includes things that changed after training.
    discuss.discuss("what's new in uv?")
    assert "WebSearch" in fake_llm.kwargs["allowed_tools"]


def test_history_is_included_so_follow_ups_work(fake_llm):
    discuss.discuss(
        "why?",
        [{"role": "user", "content": "should I use WAL mode"},
         {"role": "assistant", "content": "yes, for concurrent readers"}],
    )
    assert "concurrent readers" in fake_llm.prompt
    assert "why?" in fake_llm.prompt


def test_a_project_puts_the_agent_in_that_repo(fake_llm, tmp_path):
    discuss.discuss("how does auth work here?", project_path=str(tmp_path),
                    project_name="ResolveMind")
    # cwd is what lets Read/Grep/Glob answer from the real code.
    assert fake_llm.kwargs["cwd"] == str(tmp_path)
    assert "ResolveMind" in fake_llm.prompt


def test_without_a_project_it_still_works(fake_llm):
    """Most discussion is not about a specific repo, and must not require one."""
    reply = discuss.discuss("what is a monad")
    assert reply.reply
    assert fake_llm.kwargs["cwd"], "no working directory was given to the CLI"
    assert "PROJECT IN SCOPE" not in fake_llm.prompt


def test_the_prompt_says_it_is_a_discussion_not_a_work_order(fake_llm):
    discuss.discuss("hi")
    assert "discussion, not a work order" in fake_llm.prompt


# --- bot routing ---------------------------------------------------------


def test_the_bot_routes_questions_to_discussion():
    """The routing decision lives in on_message; this pins the rule it uses."""
    import pathlib
    import re

    src = pathlib.Path("eng_crew/discord_bot.py").read_text(encoding="utf-8")
    assert "if is_question(content) or not matched:" in src, (
        "the talk-by-default rule is not in the message handler"
    )
    # The old dead end must be gone: an unrecognised message is now a conversation.
    assert "I couldn't tell which project" not in src
    assert re.search(r"_handle_discuss\(message, content", src)
