"""Tests for stream-json progress reporting and --resume continuity.

These cover the parsing and command construction, which is where this breaks
silently. The live behaviour (a real CLI session, resumed) is verified by
running the provider against the CLI, not here — these keep the contract
between the CLI's event shapes and what the dashboard is told.
"""
from __future__ import annotations

from eng_crew import verify
from eng_crew.providers.claude_cli import (
    TRUNCATION_PREFIX,
    ClaudeCLIProvider,
    summarize_event,
)

# Event shapes below are taken from real CLI output, not invented.


def test_init_event_reports_the_model():
    line = summarize_event(
        {"type": "system", "subtype": "init", "model": "claude-opus-5", "session_id": "s"}
    )
    assert "claude-opus-5" in line


def test_tool_use_event_names_the_tool_and_target():
    line = summarize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/app.py"}}
        ]},
    })
    assert line.startswith("Edit")
    assert "src/app.py" in line


def test_tool_use_falls_back_to_command_for_bash():
    line = summarize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}
        ]},
    })
    assert "pytest -q" in line


def test_tool_use_wins_over_text_in_the_same_message():
    line = summarize_event({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "Let me edit that file"},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}},
        ]},
    })
    assert line.startswith("Edit")


def test_assistant_text_is_reported_and_bounded():
    line = summarize_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x" * 500}]},
    })
    assert 0 < len(line) <= 80


def test_newlines_are_flattened_out_of_progress_lines():
    line = summarize_event({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "first\nsecond"}]},
    })
    assert "\n" not in line


def test_result_event_reports_turn_count():
    line = summarize_event({"type": "result", "subtype": "success", "num_turns": 12})
    assert "12" in line


def test_max_turns_result_is_reported_as_a_stop():
    line = summarize_event({"type": "result", "subtype": "error_max_turns", "num_turns": 60})
    assert "turn limit" in line


def test_uninteresting_events_produce_no_progress_line():
    assert summarize_event({"type": "rate_limit_event", "session_id": "s"}) == ""
    assert summarize_event({"type": "system", "subtype": "thinking_tokens"}) == ""
    assert summarize_event({"type": "assistant", "message": {"content": []}}) == ""


def test_truncation_prefix_is_what_the_gate_looks_for():
    # If these drift apart, truncated runs silently start passing verification.
    assert TRUNCATION_PREFIX.startswith(verify.TRUNCATION_MARKER)


# --- command construction ------------------------------------------------


def _cmd(**kwargs):
    return ClaudeCLIProvider()._build_cmd("claude-opus-5", "do a thing", kwargs)


def test_command_requests_streaming_json_with_verbose():
    cmd = _cmd()
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    # stream-json is rejected by the CLI without --verbose under --print.
    assert "--verbose" in cmd


def test_resume_is_absent_unless_a_session_is_given():
    assert "--resume" not in _cmd()


def test_resume_passes_the_session_id():
    cmd = _cmd(resume_session="abc-123")
    assert cmd[cmd.index("--resume") + 1] == "abc-123"


def test_allowed_tools_and_max_turns_are_forwarded():
    cmd = _cmd(allowed_tools="Read,Edit", max_turns=42)
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Edit"
    assert cmd[cmd.index("--max-turns") + 1] == "42"


# --- progress persistence ------------------------------------------------


def _fresh_db(tmp_path, monkeypatch):
    """Point tracker at an empty DB holding one run row, and return its id.

    The schema is created with a connection this test owns and closes, rather
    than via tracker._init_db(): tracker leaves its connections open, and under
    WAL those open handles block the next writer.
    """
    import sqlite3

    from eng_crew import tracker

    db = tmp_path / "runs.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_text TEXT NOT NULL,
                project_path TEXT NOT NULL,
                started_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                current_subtask_idx INTEGER DEFAULT 0,
                current_subtask_desc TEXT
            )
        """)
        run_id = conn.execute(
            "INSERT INTO runs (task_text, project_path, started_at) VALUES (?,?,?)",
            ("a task", str(tmp_path), "now"),
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(tracker, "DB_PATH", db)
    return tracker, run_id


def _read(db_path, run_id, column):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT {column} FROM runs WHERE id=?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_update_run_progress_persists_the_line(tmp_path, monkeypatch):
    tracker, run_id = _fresh_db(tmp_path, monkeypatch)
    tracker.update_run_progress(run_id, 3, "single-agent: Edit src/app.py")
    db = tmp_path / "runs.db"
    assert _read(db, run_id, "current_subtask_idx") == 3
    assert _read(db, run_id, "current_subtask_desc") == "single-agent: Edit src/app.py"


def test_update_run_progress_truncates_a_long_line(tmp_path, monkeypatch):
    tracker, run_id = _fresh_db(tmp_path, monkeypatch)
    tracker.update_run_progress(run_id, 0, "x" * 5000)
    assert len(_read(tmp_path / "runs.db", run_id, "current_subtask_desc")) <= 500


def test_update_run_progress_is_a_noop_without_a_run_id():
    from eng_crew import tracker

    tracker.update_run_progress(0, -1, "should not raise")


def test_update_run_progress_never_raises_on_a_bad_run_id():
    from eng_crew import tracker

    tracker.update_run_progress(999_999_999, -1, "no such run")
