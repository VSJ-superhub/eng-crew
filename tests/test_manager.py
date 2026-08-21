"""Manager agent and its dashboard router.

The Manager is the conversational surface that turns a fuzzy idea into a
buildable task, so the part worth testing is what it does with the model's
reply — specifically whether it finds a build proposal in it, and whether it
refuses to invent one that isn't there. A false proposal would put a task in
front of the user that the manager never actually agreed to.

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

from eng_crew import tracker  # noqa: E402


# --- proposal parsing ---------------------------------------------------


def _block(payload: dict) -> str:
    return "```build\n" + json.dumps(payload) + "\n```"


def test_plain_conversation_yields_no_proposal():
    reply, proposal = manager._extract_proposal("What database are you using?")
    assert proposal is None
    assert reply == "What database are you using?"


def test_a_build_block_becomes_a_proposal():
    text = "Right, let's do it.\n\n" + _block(
        {"task": "Add EXIF-based renaming to the CLI", "rationale": "hooks into cli.py"}
    )
    reply, proposal = manager._extract_proposal(text)
    assert proposal["task"] == "Add EXIF-based renaming to the CLI"
    assert proposal["rationale"] == "hooks into cli.py"
    # the block is stripped out of what the user reads
    assert "```build" not in reply
    assert reply.startswith("Right, let's do it.")


def test_malformed_json_in_the_block_is_not_a_proposal():
    # Better to keep talking than to dispatch a task parsed out of broken JSON.
    text = "Sure.\n\n```build\n{\"task\": \"half a str\n```"
    reply, proposal = manager._extract_proposal(text)
    assert proposal is None
    assert "Sure." in reply


def test_a_block_with_an_empty_task_is_not_a_proposal():
    reply, proposal = manager._extract_proposal("ok\n\n" + _block({"task": "   "}))
    assert proposal is None


def test_a_block_missing_task_is_not_a_proposal():
    reply, proposal = manager._extract_proposal("ok\n\n" + _block({"rationale": "why"}))
    assert proposal is None


def test_rationale_is_optional():
    reply, proposal = manager._extract_proposal(_block({"task": "Do the thing"}))
    assert proposal == {"task": "Do the thing", "rationale": ""}


def test_empty_response_is_handled():
    assert manager._extract_proposal("") == ("", None)


def test_prose_that_merely_mentions_a_build_block_is_not_a_proposal():
    reply, proposal = manager._extract_proposal(
        "When we're ready I'll end my message with a ```build block."
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
    fake_llm.reply = "Agreed.\n\n" + _block({"task": "Build the renamer", "rationale": "r"})
    result = manager.chat("let's build it", [], str(tmp_path))
    assert result.proposal["task"] == "Build the renamer"


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


# --- dispatch -----------------------------------------------------------


def test_dispatch_returns_a_run_id_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()

    started: list[dict] = []

    def fake_run_task(**kwargs):
        started.append(kwargs)

    import eng_crew.run as run_mod

    monkeypatch.setattr(run_mod, "run_task", fake_run_task)

    run_id = manager.dispatch("build the thing", str(tmp_path))
    assert isinstance(run_id, int) and run_id > 0

    # Confirm the background thread reached the stub and not the real builder —
    # if the patch missed, this test would quietly start a paid pipeline run.
    for _ in range(100):
        if started:
            break
        time.sleep(0.02)
    assert started, "dispatch did not call the patched run_task"
    assert started[0]["task"] == "build the thing"

    # the run row exists immediately, before the build finishes
    row = tracker._connect().execute(
        "SELECT task_text FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row["task_text"] == "build the thing"
    tracker.close_connection()


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
        lambda *a, **k: manager.ManagerReply(reply="hello", proposal={"task": "t", "rationale": "r"}),
    )
    resp = client.post("/api/manager/chat", json={"message": "hi", "project_path": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.json() == {"reply": "hello", "proposal": {"task": "t", "rationale": "r"}}


def test_chat_endpoint_reports_a_failure_as_500(client, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(manager, "chat", boom)
    resp = client.post("/api/manager/chat", json={"message": "hi", "project_path": str(tmp_path)})
    assert resp.status_code == 500
    assert "claude exploded" in resp.json()["error"]


def test_dispatch_endpoint_requires_task_and_project(client, tmp_path):
    assert client.post("/api/manager/dispatch", json={"task": "", "project_path": str(tmp_path)}).status_code == 400
    assert client.post("/api/manager/dispatch", json={"task": "t", "project_path": ""}).status_code == 400


def test_dispatch_endpoint_returns_the_run_id(client, monkeypatch, tmp_path):
    monkeypatch.setattr(manager, "dispatch", lambda *a, **k: 4242)
    resp = client.post("/api/manager/dispatch", json={"task": "t", "project_path": str(tmp_path)})
    assert resp.status_code == 200
    assert resp.json() == {"run_id": 4242}


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
