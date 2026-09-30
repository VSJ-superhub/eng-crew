"""Manager agent and its dashboard router.

The Manager is the product manager: it turns a fuzzy idea into a *ticket* —
title, description, acceptance criteria — and files it in the backlog. Nothing
it does starts a build; a human moving the ticket to ready does that. So the
parts worth testing are what it makes of the model's reply (does it find a
proposal, and does it refuse to invent one that isn't there), and what accepting
that proposal writes: a backlog ticket, credited to the manager, with the
missing-criteria gap surfaced rather than swallowed.

manager.chat() imports call_llm at module level, so it already has a usable
seam; no refactor was needed to make this testable, only tests.
"""
from __future__ import annotations

import json
import time

import pytest

from eng_crew import manager
from eng_crew.providers.base import LLMResult

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tickets, tracker  # noqa: E402


# --- proposal parsing ---------------------------------------------------


def _block(payload: dict) -> str:
    return "```ticket\n" + json.dumps(payload) + "\n```"


def test_plain_conversation_yields_no_proposal():
    reply, proposal = manager._extract_proposal("What database are you using?")
    assert proposal is None
    assert reply == "What database are you using?"


def test_a_build_block_becomes_a_proposal():
    text = "Right, let's do it.\n\n" + _block(
        {"title": "Add EXIF-based renaming to the CLI", "rationale": "hooks into cli.py"}
    )
    reply, proposal = manager._extract_proposal(text)
    assert proposal["title"] == "Add EXIF-based renaming to the CLI"
    assert proposal["rationale"] == "hooks into cli.py"
    # the block is stripped out of what the user reads
    assert "```ticket" not in reply
    assert reply.startswith("Right, let's do it.")


def test_a_proposal_carries_acceptance_criteria():
    text = _block({
        "title": "Add EXIF renaming",
        "description": "rename from the capture date",
        "acceptance_criteria": ["a JPEG with EXIF is renamed", "no EXIF is left alone"],
    })
    _, proposal = manager._extract_proposal(text)
    assert proposal["title"] == "Add EXIF renaming"
    assert proposal["description"] == "rename from the capture date"
    assert proposal["acceptance_criteria"] == [
        "a JPEG with EXIF is renamed", "no EXIF is left alone",
    ]


def test_malformed_json_in_the_block_is_not_a_proposal():
    # Better to keep talking than to file a ticket parsed out of broken JSON.
    text = "Sure.\n\n```ticket\n{\"title\": \"half a str\n```"
    reply, proposal = manager._extract_proposal(text)
    assert proposal is None
    assert "Sure." in reply


def test_a_block_with_an_empty_title_is_not_a_proposal():
    reply, proposal = manager._extract_proposal("ok\n\n" + _block({"title": "   "}))
    assert proposal is None


def test_a_block_missing_a_title_is_not_a_proposal():
    reply, proposal = manager._extract_proposal("ok\n\n" + _block({"rationale": "why"}))
    assert proposal is None


def test_rationale_is_optional():
    reply, proposal = manager._extract_proposal(_block({"title": "Do the thing"}))
    assert proposal["title"] == "Do the thing"
    assert proposal["rationale"] == ""


def test_empty_response_is_handled():
    assert manager._extract_proposal("") == ("", None)


def test_prose_that_merely_mentions_a_build_block_is_not_a_proposal():
    reply, proposal = manager._extract_proposal(
        "When we're ready I'll end my message with a ```ticket block."
    )
    assert proposal is None


# --- chat() through the seam --------------------------------------------


@pytest.fixture
def fake_llm(monkeypatch):
    """Substitute the LLM call. Set .reply; inspect .kwargs and .prompt."""

    class Fake:
        def __init__(self):
            self.reply = "ok"
            self.prompt = ""
            self.kwargs: dict = {}

        def __call__(self, provider, model, prompt, **kwargs):
            self.prompt = prompt
            self.kwargs = {"provider": provider, "model": model, **kwargs}
            return LLMResult(text=self.reply, provider=provider, model=model)

    fake = Fake()
    monkeypatch.setattr(manager, "call_llm", fake)
    return fake


def test_chat_returns_reply_and_no_proposal_for_a_question(fake_llm, tmp_path):
    fake_llm.reply = "Which formats do you need?"
    result = manager.chat("rename my photos", [], str(tmp_path))
    assert result.reply == "Which formats do you need?"
    assert result.proposal is None


def test_chat_surfaces_a_proposal(fake_llm, tmp_path):
    fake_llm.reply = "Agreed.\n\n" + _block({"title": "Build the renamer", "rationale": "r"})
    result = manager.chat("let's build it", [], str(tmp_path))
    assert result.proposal["title"] == "Build the renamer"


def test_chat_never_returns_an_empty_reply(fake_llm, tmp_path):
    fake_llm.reply = ""
    assert manager.chat("hi", [], str(tmp_path)).reply == "(no response)"


def test_chat_grounds_the_call_in_the_project(fake_llm, tmp_path):
    manager.chat("hi", [], str(tmp_path))
    # cwd is what makes Read/Grep/Glob hit the real repo rather than nothing.
    assert fake_llm.kwargs["cwd"] == str(tmp_path)


def test_chat_gives_the_manager_read_only_tools(fake_llm, tmp_path):
    manager.chat("hi", [], str(tmp_path))
    tools = fake_llm.kwargs["allowed_tools"]
    assert set(tools.split(",")) == {"Read", "Grep", "Glob"}
    # The manager must never be able to write code; that is a separate step.
    for forbidden in ("Edit", "Write", "Bash"):
        assert forbidden not in tools


def test_chat_includes_the_conversation_history_in_the_prompt(fake_llm, tmp_path):
    manager.chat(
        "and dark mode",
        [{"role": "user", "content": "build a settings page"}],
        str(tmp_path),
    )
    assert "build a settings page" in fake_llm.prompt
    assert "and dark mode" in fake_llm.prompt


def test_chat_includes_project_context_when_given(fake_llm, tmp_path):
    manager.chat("hi", [], str(tmp_path), "This project is a FastAPI service")
    assert "FastAPI service" in fake_llm.prompt


# --- accepting a proposal into the backlog ------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "accept.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


PROPOSAL = {
    "title": "Add EXIF-based renaming to the CLI",
    "description": "Rename photos from their EXIF capture date.",
    "acceptance_criteria": ["a JPEG with EXIF is renamed", "a file with no EXIF is left alone"],
    "rationale": "hooks into cli.py",
}


def test_accepting_a_proposal_creates_a_backlog_ticket_and_starts_no_run(db):
    result = manager.accept(PROPOSAL, str(db))

    ticket = tracker.get_backlog_item(result["ticket_id"])
    assert ticket["status"] == tickets.BACKLOG
    assert ticket["title"] == PROPOSAL["title"]
    assert "a JPEG with EXIF is renamed" in ticket["acceptance_criteria"]
    # The whole point: a proposal is a ticket, not a build.
    assert tracker.get_recent_runs(limit=10) == []


def test_the_tickets_history_records_the_manager_as_its_creator(db):
    result = manager.accept(PROPOSAL, str(db))
    events = tickets.events(result["ticket_id"])
    assert events, "the ticket has no history"
    assert events[0]["actor"] == "manager"
    assert events[0]["to_status"] == tickets.BACKLOG


def test_a_proposal_without_acceptance_criteria_surfaces_the_gap(db):
    result = manager.accept({"title": "Do something vague"}, str(db))

    # Not silently accepted as buildable: the caller is told what is missing...
    assert result["needs_acceptance_criteria"] is True
    assert "acceptance criteria" in result["warning"].lower()
    # ...and the lifecycle still refuses to let it become ready.
    with pytest.raises(tickets.TransitionRefused, match="acceptance criteria"):
        tickets.move(result["ticket_id"], tickets.READY, "human")


def test_a_proposal_with_criteria_reports_no_gap(db):
    result = manager.accept(PROPOSAL, str(db))
    assert result["needs_acceptance_criteria"] is False
    assert result["warning"] == ""


def test_a_proposal_without_a_title_is_not_a_ticket(db):
    with pytest.raises(ValueError):
        manager.accept({"acceptance_criteria": ["something"]}, str(db))


def test_approving_the_ticket_to_ready_and_running_it_builds_the_ticket(db, monkeypatch):
    """The manager stops at the backlog; a human moving it to ready is what
    releases it into the existing ticket queue."""
    from eng_crew import run as run_mod

    ticket_id = manager.accept(PROPOSAL, str(db))["ticket_id"]
    tickets.move(ticket_id, tickets.READY, "human")
    assert [t["id"] for t in tickets.queue(str(db))] == [ticket_id]

    seen = {}

    def fake_pipeline(*, task, project_path, settings, run_id, base_branch=None):
        seen.update(task=task, run_id=run_id)
        tracker.finish_run(run_id, status="completed")
        return {}

    monkeypatch.setattr(run_mod, "run_pipeline", fake_pipeline)
    run_mod.run_ticket(ticket_id, actor="queue")

    assert PROPOSAL["title"] in seen["task"]
    assert "a JPEG with EXIF is renamed" in seen["task"], "the criteria never reached the builder"
    assert tracker.get_backlog_item(ticket_id)["status"] == tickets.IN_REVIEW


# --- the Discord /ideate surface ----------------------------------------


def test_discord_ideate_accept_creates_a_backlog_ticket_and_no_run(db):
    discord_bot = pytest.importorskip("eng_crew.discord_bot")

    message = discord_bot.accept_proposal({"path": str(db), "name": "proj"}, PROPOSAL)

    ticket = tracker.list_project_tickets(str(db))[0]
    assert ticket["status"] == tickets.BACKLOG
    assert str(ticket["id"]) in message
    assert tracker.get_recent_runs(limit=10) == []


def test_discord_ideate_accept_surfaces_a_missing_criteria_gap(db):
    discord_bot = pytest.importorskip("eng_crew.discord_bot")

    message = discord_bot.accept_proposal({"path": str(db), "name": "proj"}, {"title": "vague"})
    assert "acceptance criteria" in message.lower()


# --- router -------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "mgr.db")
    tracker.close_connection()
    tracker._init_db()
    from eng_crew.dashboard.app import app

    with TestClient(app) as c:
        yield c
    tracker.close_connection()


def test_chat_endpoint_requires_a_project_path(client):
    resp = client.post("/api/manager/chat", json={"message": "hi", "project_path": ""})
    assert resp.status_code == 400
    assert "project_path" in resp.json()["error"]


def test_chat_endpoint_returns_reply_and_proposal(client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        manager, "chat",
        lambda *a, **k: manager.ManagerReply(reply="hello", proposal={"title": "t", "rationale": "r"}),
    )
    resp = client.post("/api/manager/chat", json={"message": "hi", "project_path": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "hello", "proposal": {"title": "t", "rationale": "r"}}


def test_chat_endpoint_reports_a_failure_as_500(client, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(manager, "chat", boom)
    resp = client.post("/api/manager/chat", json={"message": "hi", "project_path": str(tmp_path)})
    assert resp.status_code == 500
    assert "claude exploded" in resp.json()["error"]


def test_accept_endpoint_requires_a_title_and_project(client, tmp_path):
    assert client.post("/api/manager/accept", json={"title": "", "project_path": str(tmp_path)}).status_code == 400
    assert client.post("/api/manager/accept", json={"title": "t", "project_path": ""}).status_code == 400


def test_accept_endpoint_files_a_ticket_and_starts_no_run(client, tmp_path):
    resp = client.post("/api/manager/accept", json={
        "title": "Add EXIF-based renaming",
        "description": "from the capture date",
        "acceptance_criteria": ["a JPEG with EXIF is renamed"],
        "project_path": str(tmp_path),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" not in body
    assert body["needs_acceptance_criteria"] is False
    assert body["ticket"]["status"] == tickets.BACKLOG
    assert tracker.get_recent_runs(limit=10) == []


def test_accept_endpoint_reports_a_missing_criteria_gap(client, tmp_path):
    resp = client.post("/api/manager/accept", json={
        "title": "Something vague", "project_path": str(tmp_path),
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_acceptance_criteria"] is True
    assert "acceptance criteria" in body["warning"].lower()


def test_remember_skips_an_empty_session(client, tmp_path):
    resp = client.post("/api/manager/remember", json={"project_path": str(tmp_path), "history": []})
    assert resp.status_code == 200
    assert resp.json()["skipped"] is True


def test_remember_calls_through_for_a_real_session(client, monkeypatch, tmp_path):
    called: list = []
    monkeypatch.setattr(manager, "remember", lambda *a, **k: called.append(a))

    resp = client.post("/api/manager/remember", json={
        "project_path": str(tmp_path),
        "history": [{"role": "user", "content": "an idea"}],
    })
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert called


def test_remember_failure_is_reported_not_swallowed(client, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("vision store unavailable")

    monkeypatch.setattr(manager, "remember", boom)
    resp = client.post("/api/manager/remember", json={
        "project_path": str(tmp_path),
        "history": [{"role": "user", "content": "an idea"}],
    })
    assert resp.status_code == 500
