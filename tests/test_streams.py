"""Server-sent event streams.

The dashboard's live views are all SSE, and none of it was tested because a
stream has no single response to assert on. The approach here: read a bounded
number of frames and close, and prefer the streams that terminate on their own —
the run event stream ends when the run reaches a terminal status, which makes it
fully consumable in a test.

Each frame is `data: <json>\\n\\n`. Malformed framing is the classic SSE bug: the
browser silently shows nothing, so it is worth asserting the wire format itself.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from eng_crew import tracker  # noqa: E402
from eng_crew.dashboard.routers import intake, runs as runs_router  # noqa: E402


@pytest.fixture
def db_only(tmp_path, monkeypatch):
    """Tracker pointed at an empty DB, without an HTTP client."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "streams.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "streams.db")
    tracker.close_connection()
    tracker._init_db()
    from eng_crew.dashboard.app import app

    with TestClient(app) as c:
        yield c
    tracker.close_connection()


def _frames(response, limit=40):
    """Parse `data:` frames off an SSE response, stopping at limit."""
    out = []
    for line in response.iter_lines():
        if not line:
            continue
        assert line.startswith("data: "), f"not an SSE frame: {line[:60]!r}"
        out.append(json.loads(line[len("data: "):]))
        if len(out) >= limit:
            break
    return out


# --- run event stream (terminates on its own) ---------------------------


def test_event_stream_of_a_finished_run_ends_with_done(client, tmp_path):
    run_id = tracker.create_run("a task", str(tmp_path))
    tracker.finish_run(run_id, status="completed", final_summary="all good")

    with client.stream("GET", f"/api/{run_id}/events/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        frames = _frames(r)

    # A terminal run must close the stream rather than poll forever.
    assert frames[-1] == {"done": True}


def test_event_stream_reports_the_run_status(client, tmp_path):
    run_id = tracker.create_run("a task", str(tmp_path))
    tracker.finish_run(run_id, status="failed", final_summary="broke")

    with client.stream("GET", f"/api/{run_id}/events/stream") as r:
        frames = _frames(r)

    statuses = [f["run"]["status"] for f in frames if "run" in f]
    assert statuses and statuses[-1] == "failed"


def test_event_stream_replays_events_recorded_before_the_client_connected(client, tmp_path):
    from eng_crew.providers.base import LLMResult

    run_id = tracker.create_run("a task", str(tmp_path))
    tracker.log_event(run_id, 0, "single_agent",
                      LLMResult(text="did the thing", provider="claude_cli", model="m"))
    tracker.finish_run(run_id, status="completed")

    with client.stream("GET", f"/api/{run_id}/events/stream") as r:
        frames = _frames(r)

    agents = [f["event"]["agent_name"] for f in frames if "event" in f]
    assert "single_agent" in agents, "a client connecting late saw no history"


def test_event_stream_of_an_unknown_run_closes(client):
    with client.stream("GET", "/api/999999/events/stream") as r:
        frames = _frames(r, limit=3)
    assert {"done": True} in frames


# --- status stream (infinite) -------------------------------------------
#
# This one never ends, and TestClient cannot be used on it: breaking out of
# iter_lines does not cancel the server-side generator, so the test client
# blocks forever on teardown. Drive the response's body_iterator directly and
# close it instead — which is also what a disconnecting browser causes.


def _read_frames(make_response, count=1):
    """Await the endpoint, take `count` SSE frames, then close the generator."""
    async def go():
        response = await make_response()
        assert response.media_type == "text/event-stream"
        frames, buffer = [], ""
        iterator = response.body_iterator
        try:
            async for chunk in iterator:
                buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
                for line in buffer.splitlines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[len("data: "):]))
                buffer = ""
                if len(frames) >= count:
                    break
        finally:
            await iterator.aclose()
        return response, frames

    return asyncio.run(go())


def test_status_stream_sends_a_dashboard_payload(db_only, tmp_path):
    running = tracker.create_run("in flight", str(tmp_path))
    done = tracker.create_run("finished", str(tmp_path))
    tracker.finish_run(done, status="completed")

    _, frames = _read_frames(runs_router.api_status_stream)
    payload = frames[0]
    assert set(payload) >= {"active_runs", "recent_runs", "cost_by_model"}

    # The two lists are disjoint: get_recent_runs excludes status 'running',
    # so a live run appears only under active_runs. The dashboard renders them
    # as separate sections and would double-count if that ever changed.
    assert [r["id"] for r in payload["active_runs"]] == [running]
    assert [r["id"] for r in payload["recent_runs"]] == [done]


def test_status_stream_sets_no_cache_headers(db_only):
    # Without these a proxy buffers the stream and the live view stalls.
    response, _ = _read_frames(runs_router.api_status_stream)
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_status_stream_keeps_emitting(db_only):
    # A live view depends on more than the first frame arriving.
    _, frames = _read_frames(runs_router.api_status_stream, count=2)
    assert len(frames) == 2


# --- intake chat stream -------------------------------------------------


@pytest.fixture
def fake_stream(monkeypatch):
    """Replace the streaming CLI call with a canned async generator."""
    captured: dict = {}

    def make(frames):
        async def gen(prompt):
            captured["prompt"] = prompt
            for f in frames:
                yield f

        monkeypatch.setattr(intake, "_run_claude_streaming", gen)
        return captured

    return make


def test_intake_chat_streams_assistant_text(client, fake_stream):
    fake_stream([
        'data: {"text": "Hello"}\n\n',
        'data: {"text": " there"}\n\n',
        "data: [DONE]\n\n",
    ])

    with client.stream("POST", "/api/intake/chat", json={"message": "hi", "history": []}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = r.read().decode()

    assert '"text": "Hello"' in body
    assert body.rstrip().endswith("[DONE]")


def test_intake_chat_passes_the_conversation_to_the_model(client, fake_stream):
    captured = fake_stream(["data: [DONE]\n\n"])

    with client.stream("POST", "/api/intake/chat", json={
        "message": "and dark mode",
        "history": [{"role": "user", "content": "build a settings page"}],
    }) as r:
        r.read()

    assert "build a settings page" in captured["prompt"]
    assert "and dark mode" in captured["prompt"]


def test_intake_chat_includes_project_details_in_the_system_prompt(client, fake_stream):
    captured = fake_stream(["data: [DONE]\n\n"])

    with client.stream("POST", "/api/intake/chat", json={
        "message": "hi",
        "history": [],
        "project_name": "Photo Renamer",
        "tech_stack": ["python", "click"],
    }) as r:
        r.read()

    assert "Photo Renamer" in captured["prompt"]
    assert "click" in captured["prompt"]


def test_intake_chat_forwards_an_error_frame(client, fake_stream):
    # The generator reports CLI failures in-band; the endpoint must not swallow it.
    fake_stream([
        'data: {"error": "claude CLI not found"}\n\n',
        "data: [DONE]\n\n",
    ])

    with client.stream("POST", "/api/intake/chat", json={"message": "hi", "history": []}) as r:
        body = r.read().decode()

    assert "claude CLI not found" in body
