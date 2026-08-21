"""Intake endpoint tests, using the LLM seam.

Intake turns what a user types into a project, and every path through it used to
require spawning the real CLI and paying for a call — so none of it was tested.
These substitute `intake.collect_completion` with a fake, which makes the
interesting part testable: not what the model says, but what the endpoint does
with what it says.

Models return prose around JSON, empty strings, truncated objects, and
occasionally nothing at all. Each of those is a case here.
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tracker  # noqa: E402
from eng_crew.dashboard.routers import intake  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "intake.db")
    tracker.close_connection()
    tracker._init_db()
    from eng_crew.dashboard.app import app

    with TestClient(app) as c:
        yield c
    tracker.close_connection()


@pytest.fixture
def llm(monkeypatch):
    """Swap the seam for a fake. Set .reply, read .prompts."""

    class Fake:
        def __init__(self):
            self.reply = ""
            self.prompts: list[str] = []
            self.raises: Exception | None = None

        async def __call__(self, prompt, model=None):
            self.prompts.append(prompt)
            if self.raises:
                raise self.raises
            return self.reply

    fake = Fake()
    monkeypatch.setattr(intake, "collect_completion", fake)
    # OLLAMA_SUMMARIZER_MODEL short-circuits to a different backend when set.
    monkeypatch.setattr(intake, "OLLAMA_SUMMARIZER_MODEL", "")
    return fake


HISTORY = [
    {"role": "user", "content": "I want a CLI that renames photos by EXIF date"},
    {"role": "assistant", "content": "What formats?"},
    {"role": "user", "content": "JPEG and HEIC"},
]


# --- /extract: the happy path and the shapes models actually return ------


def test_extract_parses_clean_json(client, llm):
    llm.reply = json.dumps({
        "title": "EXIF photo renamer",
        "description": "A CLI that renames photos by EXIF date",
        "tech_stack": ["python"],
    })
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "EXIF photo renamer"
    assert body["tech_stack"] == ["python"]


def test_extract_tolerates_prose_around_the_json(client, llm):
    # Models routinely do this despite being told not to.
    llm.reply = (
        "Sure! Here's the JSON you asked for:\n\n"
        '{"title": "Renamer", "description": "d", "tech_stack": []}\n\n'
        "Let me know if you'd like changes."
    )
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamer"


def test_extract_tolerates_markdown_fences(client, llm):
    llm.reply = '```json\n{"title": "Fenced", "description": "d", "tech_stack": []}\n```'
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Fenced"


def test_extract_sends_the_conversation_to_the_model(client, llm):
    llm.reply = '{"title": "t", "description": "d", "tech_stack": []}'
    client.post("/api/intake/extract", json={"history": HISTORY})
    prompt = llm.prompts[0]
    assert "EXIF date" in prompt and "JPEG and HEIC" in prompt


# --- /extract: the failure modes ----------------------------------------


def test_extract_falls_back_to_the_last_user_message_on_garbage(client, llm):
    llm.reply = "I'm sorry, I can't help with that."
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200, "a bad model reply must not 500 the endpoint"
    # The user's own words are a better answer than an error page.
    assert resp.json()["title"] == "JPEG and HEIC"
    assert resp.json()["tech_stack"] == []


def test_extract_falls_back_on_an_empty_reply(client, llm):
    llm.reply = ""
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    assert resp.json()["description"] == "JPEG and HEIC"


def test_extract_falls_back_on_truncated_json(client, llm):
    llm.reply = '{"title": "Half a resp'
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    assert resp.json()["title"] == "JPEG and HEIC"


def test_extract_survives_the_llm_call_raising(client, llm):
    llm.raises = RuntimeError("claude CLI not found")
    resp = client.post("/api/intake/extract", json={"history": HISTORY})
    assert resp.status_code == 200
    assert resp.json()["title"] == "JPEG and HEIC"


def test_extract_with_no_history_does_not_crash(client, llm):
    llm.reply = "not json"
    resp = client.post("/api/intake/extract", json={"history": []})
    assert resp.status_code == 200
    assert resp.json()["title"] == ""


def test_extract_title_is_bounded(client, llm):
    llm.reply = "not json"
    long_message = "x" * 500
    resp = client.post("/api/intake/extract", json={
        "history": [{"role": "user", "content": long_message}],
    })
    assert len(resp.json()["title"]) <= 80


# --- /parse-markdown ----------------------------------------------------


def test_parse_markdown_returns_the_task_list(client, llm):
    llm.reply = json.dumps({
        "tasks": [
            {"title": "Add login", "description": "email and password"},
            {"title": "Add logout", "description": ""},
        ],
        "summary": "Two auth tasks",
    })
    resp = client.post("/api/intake/parse-markdown", json={"content": "# Plan\n- login\n- logout"})
    assert resp.status_code == 200
    body = resp.json()
    assert [t["title"] for t in body["tasks"]] == ["Add login", "Add logout"]


def test_parse_markdown_includes_the_document_in_the_prompt(client, llm):
    llm.reply = '{"tasks": [], "summary": ""}'
    client.post("/api/intake/parse-markdown", json={"content": "# My Very Specific Heading"})
    assert "My Very Specific Heading" in llm.prompts[0]


def test_parse_markdown_reports_an_error_on_garbage(client, llm):
    llm.reply = "no json here at all"
    resp = client.post("/api/intake/parse-markdown", json={"content": "# Plan"})
    assert resp.status_code == 500
    assert "error" in resp.json()


# --- /save-architecture: no LLM involved --------------------------------


def test_save_architecture_writes_the_file(client, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    resp = client.post("/api/intake/save-architecture", json={
        "project_path": str(project),
        "content": "# Architecture\n",
    })
    assert resp.status_code == 200
    assert (project / "ARCHITECTURE.md").read_text(encoding="utf-8") == "# Architecture\n"


def test_save_architecture_rejects_a_missing_directory(client, tmp_path):
    resp = client.post("/api/intake/save-architecture", json={
        "project_path": str(tmp_path / "nope"),
        "content": "x",
    })
    assert resp.status_code == 400
    assert "not found" in resp.json()["error"].lower()


# --- the seam itself ----------------------------------------------------


def test_collect_completion_returns_empty_when_the_cli_is_absent(monkeypatch):
    """A missing CLI must not raise into the endpoint; callers fall back on "".
    """
    import asyncio

    async def boom(*a, **kw):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(intake.asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(intake.collect_completion("hi")) == ""


def test_collect_completion_unwraps_the_cli_json_envelope(monkeypatch):
    import asyncio

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "the answer"}).encode(), b""

    async def fake_exec(*a, **kw):
        return FakeProc()

    monkeypatch.setattr(intake.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(intake.collect_completion("hi")) == "the answer"


def test_collect_completion_returns_empty_on_a_failed_call(monkeypatch):
    import asyncio

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"boom"

    async def fake_exec(*a, **kw):
        return FakeProc()

    monkeypatch.setattr(intake.asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(intake.collect_completion("hi")) == ""


def test_collect_completion_passes_the_model_through(monkeypatch):
    import asyncio

    seen: dict = {}

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": "ok"}).encode(), b""

    async def fake_exec(*cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(intake.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(intake.collect_completion("hi", model="claude-sonnet-5"))
    assert "claude-sonnet-5" in seen["cmd"]
    # intake calls must never be able to touch the filesystem
    assert "none" in seen["cmd"], "intake must run with --allowedTools none"
