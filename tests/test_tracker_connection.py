"""The tracker connection must be reused, not re-opened per call.

Every call site used to open a fresh sqlite connection and nothing closed it —
`with _connect() as conn:` commits but leaves the handle open. In a long-lived
dashboard process the handles accumulated, and under WAL the open handles block
later writers with "database is locked".
"""
from __future__ import annotations

import gc
import sqlite3
import threading

import pytest

from eng_crew import tracker


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


def _live_connections() -> int:
    return len([o for o in gc.get_objects() if isinstance(o, sqlite3.Connection)])


def test_repeated_calls_reuse_one_connection(db):
    assert len({id(tracker._connect()) for _ in range(50)}) == 1


def test_many_calls_do_not_accumulate_handles(db):
    before = _live_connections()
    for _ in range(100):
        tracker._connect()
    assert _live_connections() <= before + 1


def test_each_thread_gets_its_own_connection(db):
    # sqlite connections are not safe to share across threads concurrently.
    seen: dict[int, int] = {}

    def worker(n: int) -> None:
        seen[n] = id(tracker._connect())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(seen.values())) == 4


def test_moving_the_db_path_opens_a_new_connection(db, tmp_path, monkeypatch):
    first = tracker._connect()
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "elsewhere.db")
    assert tracker._connect() is not first


def test_writes_keep_working_across_many_operations(db):
    conn = tracker._connect()
    with conn:
        run_id = conn.execute(
            "INSERT INTO runs (task_text, project_path, started_at, status) VALUES (?,?,?,?)",
            ("t", str(db), "now", "running"),
        ).lastrowid

    # A streaming run writes progress many times; none of these may deadlock.
    for i in range(200):
        tracker.update_run_progress(run_id, i, f"step {i}")

    row = tracker._connect().execute(
        "SELECT current_subtask_idx FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    assert row[0] == 199


def test_close_connection_is_idempotent_and_reopens(db):
    tracker._connect()
    tracker.close_connection()
    tracker.close_connection()  # must not raise on an already-closed handle
    assert tracker._connect() is not None


def test_no_tracker_function_closes_the_shared_handle():
    """A stray close() would poison the pooled connection for everything after it.

    This caught a real case: the migration block closed the handle at import,
    so every later call failed with "Cannot operate on a closed database".
    """
    import pathlib

    src = pathlib.Path("eng_crew/tracker.py").read_text(encoding="utf-8")
    closes = [
        line.strip()
        for line in src.splitlines()
        if ".close()" in line and "await" not in line
    ]
    # The only permitted close is the one inside close_connection().
    assert closes == ["conn.close()"], closes
