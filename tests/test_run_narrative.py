"""The run page as a plain-language story.

Tracing already recorded *that* a tool ran and for how long, but threw away the
one thing that makes a line readable — the file it edited, the command it ran,
the pattern it searched — and the agent's own narration was overwritten into a
single current-activity field the moment the next line arrived.

These tests pin the story end to end: the provider carries a per-call target
through the stream, the run's event handler persists both the target and every
narration line as an ordered sequence, the run detail endpoint hands all of it
back, the SSE stream delivers it while the run is still going, and the run page
leads with the narrative rather than with tool names and durations.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eng_crew import tracker
from eng_crew.providers.claude_cli import (
    TOOL_CALL_EVENT,
    ClaudeCLIProvider,
    summarize_event,
)

FRONTEND = Path(__file__).resolve().parents[1] / "eng_crew/dashboard/frontend/src"
RUN_DETAIL_TSX = FRONTEND / "pages/RunDetail.tsx"
CLIENT_TS = FRONTEND / "api/client.ts"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty tracking DB holding one running run. Yields (tracker, run_id)."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    conn = tracker._connect()
    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (task_text, project_path, started_at, status)"
            " VALUES (?,?,?,?)",
            ("a task", str(tmp_path), "2026-10-15T00:00:00Z", "running"),
        ).lastrowid
    yield tracker, run_id
    tracker.close_connection()


def _tool_use(tool_id: str, name: str, **inp) -> dict:
    return {
        "type": "assistant",
        "session_id": "s",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": name,
                                 "input": inp}]},
    }


def _tool_result(tool_id: str, *, is_error: bool = False) -> dict:
    return {
        "type": "user",
        "session_id": "s",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                 "is_error": is_error, "content": "out"}]},
    }


def _narration(text: str) -> dict:
    return {
        "type": "assistant",
        "session_id": "s",
        "message": {"content": [{"type": "text", "text": text}]},
    }


_RESULT = {"type": "result", "subtype": "success", "num_turns": 3,
           "result": "done", "session_id": "s", "total_cost_usd": 0.01}


def _run_stream(monkeypatch, events, on_event):
    provider = ClaudeCLIProvider()
    monkeypatch.setattr(provider, "_stream", lambda cmd, cwd, prompt="": iter(events))
    return provider._attempt(["claude"], ".", on_event, "prompt")


# ── Criterion 1: every tool call carries the target summarize_event derives ──

def test_tool_call_rows_carry_the_target_not_just_the_tool_name(db, monkeypatch):
    tracker, run_id = db
    from eng_crew import tracing

    stream = [
        _tool_use("t1", "Read", file_path="eng_crew/tracker.py"), _tool_result("t1"),
        _tool_use("t2", "Edit", file_path="eng_crew/tracing.py"), _tool_result("t2"),
        _tool_use("t3", "Bash", command="pytest -q"), _tool_result("t3"),
        _tool_use("t4", "Grep", pattern="log_tool_call_event"), _tool_result("t4"),
        _RESULT,
    ]
    seen: list[dict] = []

    def handler(evt):
        seen.append(evt)
        tracing.run_event_handler(run_id, "single-agent")(evt)

    _run_stream(monkeypatch, stream, handler)

    # The stream event carries it…
    streamed = [e for e in seen if e.get("type") == TOOL_CALL_EVENT]
    assert [e.get("target") for e in streamed] == [
        "eng_crew/tracker.py", "eng_crew/tracing.py", "pytest -q",
        "log_tool_call_event"]
    # …and so does the persisted row.
    rows = tracker.get_run_tool_calls(run_id)
    assert [(r["tool_name"], r["target"]) for r in rows] == [
        ("Read", "eng_crew/tracker.py"),
        ("Edit", "eng_crew/tracing.py"),
        ("Bash", "pytest -q"),
        ("Grep", "log_tool_call_event"),
    ]
    # Derived the same way the progress line already derives it.
    for evt, row in zip([e for e in stream if e.get("type") == "assistant"], rows):
        assert summarize_event(evt) == f"{row['tool_name']}: {row['target']}"
    # The API type the page reads carries it too.
    assert "target" in CLIENT_TS.read_text(encoding="utf-8").split(
        "export interface ToolCallEvent")[1].split("}")[0]


# ── Criterion 2: a Bash call reads as its command plus pass/fail ─────────────

def test_a_bash_call_shows_its_command_and_whether_it_passed(db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from eng_crew.dashboard.app import app

    tracker, run_id = db
    tracker.log_tool_call_event(run_id, -1, "Bash", "2026-10-15T00:00:01Z", 3200,
                                "error", target="pytest -q tests/test_verify.py")

    with TestClient(app) as client:
        body = client.get(f"/api/run/{run_id}").json()

    [call] = body["tool_calls"]
    assert call["tool_name"] == "Bash"
    assert call["target"] == "pytest -q tests/test_verify.py"
    assert call["outcome"] == "error"
    # And the page renders the target, not only the name and the duration.
    assert "target" in RUN_DETAIL_TSX.read_text(encoding="utf-8")


# ── Criterion 3: narration is a sequence, not one overwritten field ──────────

def test_narration_is_kept_as_a_timestamped_sequence(db, monkeypatch):
    tracker, run_id = db
    from eng_crew import tracing

    _run_stream(
        monkeypatch,
        [_narration("Reading the tracker to find where tool calls are written."),
         _tool_use("t1", "Read", file_path="eng_crew/tracker.py"), _tool_result("t1"),
         _narration("Now adding the target column and its migration."),
         _tool_use("t2", "Edit", file_path="eng_crew/tracker.py"), _tool_result("t2"),
         _RESULT],
        tracing.run_event_handler(run_id, "single-agent"),
    )

    lines = tracker.get_run_narration(run_id)
    assert [n["text"] for n in lines] == [
        "Reading the tracker to find where tool calls are written.",
        "Now adding the target column and its migration.",
    ]
    assert all(n["created_at"] for n in lines)
    assert all(n["run_id"] == run_id for n in lines)
    # The earlier line survives the later one — the single current-activity
    # field only ever holds the last thing that happened.
    desc = tracker._connect().execute(
        "SELECT current_subtask_desc FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    assert "Reading the tracker" not in (desc or "")


# ── Criterion 4: the default view is one chronological narrative feed ────────

def test_the_business_view_leads_with_a_narrative_feed(db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from eng_crew.dashboard.app import app

    tracker, run_id = db
    tracker.log_run_narration(run_id, -1, "Looking at the dashboard router.")
    tracker.log_tool_call_event(run_id, -1, "Read", "2026-10-15T00:00:02Z", 40,
                               "success", target="eng_crew/dashboard/routers/runs.py")

    with TestClient(app) as client:
        body = client.get(f"/api/run/{run_id}").json()

    # The feed's three ingredients all reach the page.
    assert [n["text"] for n in body["narration"]] == ["Looking at the dashboard router."]
    assert body["tool_calls"][0]["target"] == "eng_crew/dashboard/routers/runs.py"
    assert "stage_events" in body

    src = RUN_DETAIL_TSX.read_text(encoding="utf-8")
    assert "NarrativeFeed" in src, "the run page has no narrative feed"
    assert "narration" in src
    # It is rendered in the default (business) view, above the raw panels.
    feed = src.index("<NarrativeFeed")
    for raw in ("<ToolTrace", "<OutputViewer", "<LogViewer"):
        assert feed < src.index(raw), f"{raw} is rendered above the narrative feed"
    assert src.index("{viewMode === 'business' ? (") < feed < src.index(
        ") : viewMode === 'engineering' ? (")


# ── Criterion 5: the raw panels stay reachable, just not primary ─────────────

def test_the_raw_panels_are_still_reachable_from_the_run_page():
    src = RUN_DETAIL_TSX.read_text(encoding="utf-8")
    for raw in ("ToolTrace", "OutputViewer", "LogViewer"):
        assert raw in src, f"{raw} was removed rather than demoted"
    # Still their own views, reached by the mode switch.
    for mode in ("'business'", "'engineering'", "'logs'"):
        assert mode in src
    # The default view is the narrative one, so they are no longer the only
    # way to see what a run is doing.
    assert "useState<'business' | 'engineering' | 'logs'>('business')" in src
    assert src.index("<NarrativeFeed") < src.index("<ToolTrace")


# ── Criterion 6: it fills in live, over the existing SSE stream ──────────────

def test_the_feed_arrives_live_over_the_sse_stream(db):
    """The run is still `running`: the feed must fill in without a reload."""
    pytest.importorskip("fastapi")
    import asyncio

    from eng_crew.dashboard.routers.runs import run_event_stream

    tracker, run_id = db
    tracker.log_run_narration(run_id, -1, "Starting on the SSE stream.")
    tracker.log_tool_call_event(run_id, -1, "Bash", "2026-10-15T00:00:03Z", 1200,
                                "success", target="npm run build")

    async def drain() -> list[dict]:
        frames, stream = [], run_event_stream(run_id, poll_secs=0)
        async for chunk in stream:
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[len("data: "):]))
            if any(f.get("narration") for f in frames) and any(
                    f.get("tool_call") for f in frames):
                await stream.aclose()
                break
            assert len(frames) < 50, f"the stream never sent the feed: {frames}"
        return frames

    frames = asyncio.run(drain())

    assert [f["narration"]["text"] for f in frames if f.get("narration")] == [
        "Starting on the SSE stream."]
    assert [(f["tool_call"]["tool_name"], f["tool_call"]["target"],
             f["tool_call"]["outcome"]) for f in frames if f.get("tool_call")] == [
        ("Bash", "npm run build", "success")]
    assert not any(f.get("error") for f in frames), frames

    # The endpoint the page actually opens serves exactly this stream.
    from eng_crew.dashboard.routers import runs as runs_router

    served = {r.path for r in runs_router.router.routes}
    assert "/api/runs/{run_id}/events/stream" in served, (
        "the run page opens /api/runs/<id>/events/stream; nothing serves it")
    assert "/api/runs/${id}/events/stream" in RUN_DETAIL_TSX.read_text(
        encoding="utf-8")

    # And the page consumes both off the stream rather than only on reload.
    src = RUN_DETAIL_TSX.read_text(encoding="utf-8")
    assert "data.narration" in src
    assert "data.tool_call" in src
