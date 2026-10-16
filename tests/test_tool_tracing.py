"""Per-run tool-call tracing.

The CLI already streams a structured event for every tool the agent uses and
for its result; until now that was reduced to a progress string and thrown
away, so nothing could say what a run actually *did* — which tools, how often,
how long, how many failed.

These tests pin the chain end to end: the provider turns a tool_use/tool_result
pair into one timed event, the run's event handler writes it against the run,
the run detail endpoint hands it back in order, and none of it touches the
cost/token accounting in subtask_events or can fail a run when the database
refuses the write.
"""
from __future__ import annotations

import pytest

from eng_crew import tracker
from eng_crew.providers.claude_cli import (
    TOOL_CALL_EVENT,
    ClaudeCLIProvider,
    ToolCallTimeline,
    summarize_event,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty tracking DB holding one run row. Yields (tracker, run_id)."""
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    conn = tracker._connect()
    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (task_text, project_path, started_at, status)"
            " VALUES (?,?,?,?)",
            ("a task", str(tmp_path), "2026-10-14T00:00:00Z", "running"),
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


_RESULT = {"type": "result", "subtype": "success", "num_turns": 3,
           "result": "done", "session_id": "s", "total_cost_usd": 0.01}


def _run_stream(monkeypatch, events, on_event, *, provider=None):
    """Drive one _attempt over a canned stream, returning what it collected."""
    provider = provider or ClaudeCLIProvider()
    monkeypatch.setattr(provider, "_stream", lambda cmd, cwd, prompt="": iter(events))
    return provider._attempt(["claude"], ".", on_event, "prompt")


# ── Criterion 2: the provider passes timed tool calls through on_event ───────

def test_provider_reports_each_tool_call_with_name_duration_and_outcome(monkeypatch):
    seen: list[dict] = []
    _run_stream(
        monkeypatch,
        [_tool_use("t1", "Read", file_path="a.py"), _tool_result("t1"),
         _tool_use("t2", "Bash", command="pytest"), _tool_result("t2", is_error=True),
         _RESULT],
        seen.append,
    )
    calls = [e for e in seen if e.get("type") == TOOL_CALL_EVENT]
    assert [c["tool_name"] for c in calls] == ["Read", "Bash"]
    assert [c["outcome"] for c in calls] == ["success", "error"]
    assert all(c["started_at"] for c in calls)
    assert all(isinstance(c["duration_ms"], int) and c["duration_ms"] >= 0 for c in calls)


def test_a_tool_call_with_no_result_is_still_reported_when_the_stream_ends(monkeypatch):
    """A run cut off mid-tool still says the tool ran — silence would lose it."""
    seen: list[dict] = []
    _run_stream(monkeypatch, [_tool_use("t1", "Edit", file_path="a.py"), _RESULT],
                seen.append)
    calls = [e for e in seen if e.get("type") == TOOL_CALL_EVENT]
    assert [(c["tool_name"], c["outcome"]) for c in calls] == [("Edit", "incomplete")]


def test_the_raw_stream_events_still_reach_on_event(monkeypatch):
    """Tracing is additive: progress reporting reads the same callback."""
    seen: list[dict] = []
    _run_stream(monkeypatch, [_tool_use("t1", "Read", file_path="a.py"),
                              _tool_result("t1"), _RESULT], seen.append)
    assert [e["type"] for e in seen if e["type"] != TOOL_CALL_EVENT] == [
        "assistant", "user", "result"]


def test_a_synthetic_tool_call_event_adds_no_progress_line():
    """summarize_event must not start narrating the events tracing added."""
    assert summarize_event({"type": TOOL_CALL_EVENT, "tool_name": "Read",
                            "outcome": "success", "duration_ms": 5}) == ""


def test_the_timeline_pairs_results_to_their_own_tool_use():
    """Interleaved calls must not swap durations or outcomes."""
    timeline = ToolCallTimeline()
    assert timeline.observe(_tool_use("a", "Read")) == []
    assert timeline.observe(_tool_use("b", "Bash")) == []
    [second] = timeline.observe(_tool_result("b", is_error=True))
    [first] = timeline.observe(_tool_result("a"))
    assert (second["tool_name"], second["outcome"]) == ("Bash", "error")
    assert (first["tool_name"], first["outcome"]) == ("Read", "success")
    assert timeline.flush() == []


# ── Criterion 1: the rows land, and the cost accounting is untouched ─────────

def test_tool_calls_are_persisted_against_the_run(db, monkeypatch):
    tracker, run_id = db
    from eng_crew import tracing

    _run_stream(
        monkeypatch,
        [_tool_use("t1", "Read", file_path="a.py"), _tool_result("t1"),
         _tool_use("t2", "Bash", command="pytest"), _tool_result("t2", is_error=True),
         _RESULT],
        tracing.run_event_handler(run_id, "single-agent", subtask_idx=2),
    )
    rows = tracker.get_run_tool_calls(run_id)
    assert [(r["tool_name"], r["outcome"]) for r in rows] == [
        ("Read", "success"), ("Bash", "error")]
    assert all(r["run_id"] == run_id and r["subtask_idx"] == 2 for r in rows)
    assert all(r["started_at"] for r in rows)
    assert all(r["duration_ms"] >= 0 for r in rows)


def test_tracing_adds_no_rows_to_the_cost_and_token_accounting(db, monkeypatch):
    """subtask_events is what a ticket's spend is computed from; a tool call is
    not a model call and must never appear there."""
    tracker, run_id = db
    from eng_crew import tracing

    _run_stream(
        monkeypatch,
        [_tool_use("t1", "Read", file_path="a.py"), _tool_result("t1"), _RESULT],
        tracing.run_event_handler(run_id, "single-agent"),
    )
    assert tracker.get_run_events(run_id) == []
    assert tracker.get_run_cost(run_id) == 0.0


def test_the_progress_line_is_still_written(db, monkeypatch):
    """Tracing shares the callback with progress reporting; both still happen.
    The run row holds the latest line, so the tool event is checked as the
    last one before the result."""
    tracker, run_id = db
    from eng_crew import tracing

    _run_stream(monkeypatch, [_tool_use("t1", "Read", file_path="a.py"),
                              _tool_result("t1")],
                tracing.run_event_handler(run_id, "single-agent"))
    desc = tracker._connect().execute(
        "SELECT current_subtask_desc FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    assert "single-agent: Read" in desc
    assert tracker.get_run_tool_calls(run_id)[0]["tool_name"] == "Read"


# ── Criterion 2 (wiring): every tier that uses on_event records tool calls ───

@pytest.mark.parametrize("module_name", [
    "eng_crew.agents.single_agent",
    "eng_crew.agents.simple_executor",
])
def test_the_execution_tiers_record_tool_calls(db, monkeypatch, module_name):
    import importlib

    tracker, run_id = db
    module = importlib.import_module(module_name)
    _run_stream(monkeypatch,
                [_tool_use("t1", "Edit", file_path="a.py"), _tool_result("t1"), _RESULT],
                module._progress_reporter(run_id, "tier"))
    assert [r["tool_name"] for r in tracker.get_run_tool_calls(run_id)] == ["Edit"]


def test_the_verifier_repair_pass_records_tool_calls(db, monkeypatch):
    from eng_crew.agents.verifier import VerifierAgent

    tracker, run_id = db
    agent = VerifierAgent.__new__(VerifierAgent)
    _run_stream(monkeypatch,
                [_tool_use("t1", "Bash", command="pytest"), _tool_result("t1"), _RESULT],
                agent._progress(run_id, "repair 1"))
    assert [r["tool_name"] for r in tracker.get_run_tool_calls(run_id)] == ["Bash"]


def test_the_sandboxed_reviewer_traces_its_own_call(db, monkeypatch):
    """The reviewer is a CLI call too; its tool calls belong to the run it judges."""
    from eng_crew import review
    from eng_crew.config import settings
    from eng_crew.providers.base import LLMResult

    tracker, run_id = db
    captured: dict = {}

    def fake_call(self, prompt, **kwargs):
        captured.update(kwargs)
        return LLMResult(text="APPROVE — fine")

    monkeypatch.setattr("eng_crew.agents.base.BaseAgent.call", fake_call)
    review._ask({"title": "t", "description": "d", "acceptance_criteria": "c"},
                None, "diff", run_id=run_id, settings=settings,
                reviewer={"provider": "claude_cli", "model": "m", "sandbox": True})

    on_event = (captured.get("extra") or {}).get("on_event")
    assert callable(on_event), "the reviewer passes no on_event, so it cannot be traced"
    _run_stream(monkeypatch, [_tool_use("t1", "Read", file_path="a.py"),
                              _tool_result("t1"), _RESULT], on_event)
    assert [r["tool_name"] for r in tracker.get_run_tool_calls(run_id)] == ["Read"]


# ── Criterion 7: instrumentation can never fail a run ────────────────────────

def test_a_failing_trace_write_does_not_break_the_call(db, monkeypatch):
    tracker, run_id = db
    from eng_crew import tracing

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(tracker, "log_tool_call_event", boom)
    got = _run_stream(monkeypatch,
                      [_tool_use("t1", "Read", file_path="a.py"), _tool_result("t1"),
                       _RESULT],
                      tracing.run_event_handler(run_id, "single-agent"))
    assert got["text"] == "done"
    assert got["got_result"] is True


def test_logging_a_tool_call_never_raises_on_a_bad_run(db):
    tracker, _ = db
    tracker.log_tool_call_event(999_999_999, -1, "Read", "2026-10-14T00:00:00Z", 5,
                                "success")


# ── Criterion 3: the run detail endpoint returns the trace ───────────────────

def test_run_detail_returns_the_tool_calls_in_order(db):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from eng_crew.dashboard.app import app

    tracker, run_id = db
    for i, (name, outcome) in enumerate(
            [("Read", "success"), ("Bash", "error"), ("Edit", "success")]):
        tracker.log_tool_call_event(run_id, -1, name, f"2026-10-14T00:00:0{i}Z",
                                    100 + i, outcome)

    with TestClient(app) as client:
        body = client.get(f"/api/run/{run_id}").json()

    calls = body["tool_calls"]
    assert [c["tool_name"] for c in calls] == ["Read", "Bash", "Edit"]
    assert [c["outcome"] for c in calls] == ["success", "error", "success"]
    assert [c["duration_ms"] for c in calls] == [100, 101, 102]


# ── Criterion 4: the run page renders the trace ──────────────────────────────

def test_run_detail_page_renders_a_tool_call_trace():
    """No JS test runner is set up here, so this pins the source contract: the
    page must read tool_calls off the response and show name/outcome/duration."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / (
        "eng_crew/dashboard/frontend/src/pages/RunDetail.tsx")
    text = src.read_text(encoding="utf-8")
    assert "tool_calls" in text
    for field in ("tool_name", "outcome", "duration_ms"):
        assert field in text, f"the trace timeline never shows {field}"
