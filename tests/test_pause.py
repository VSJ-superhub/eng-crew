"""Pause has to actually stop a run.

The endpoint used to write a `pause_requested` flag that no code read, so the
button did nothing. The pipeline now checks it between graph nodes — between,
not inside, because an agent turn is one long CLI call that cannot be
interrupted halfway.
"""
from __future__ import annotations

import threading
import time

import pytest

from eng_crew import pipeline, tracker
from eng_crew.config import Settings


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    yield tmp_path
    tracker.close_connection()


@pytest.fixture
def run_id(db):
    return tracker.create_run("a task", str(db))


def _settings(**over):
    s = Settings()
    s.pause_poll_seconds = 0.01          # keep the tests quick
    s.pause_timeout_seconds = 5.0
    for k, v in over.items():
        setattr(s, k, v)
    return s


# --- the wait itself ----------------------------------------------------


def test_an_unpaused_run_is_not_delayed(run_id):
    start = time.time()
    pipeline._wait_while_paused(run_id, _settings())
    assert time.time() - start < 0.5


def test_no_run_id_is_a_noop():
    pipeline._wait_while_paused(None, _settings())


def test_a_paused_run_waits_until_it_is_released(run_id):
    tracker.set_pause_requested(run_id, True)

    def release():
        time.sleep(0.2)
        tracker.set_pause_requested(run_id, False)

    threading.Thread(target=release, daemon=True).start()

    start = time.time()
    pipeline._wait_while_paused(run_id, _settings())
    waited = time.time() - start

    assert waited >= 0.15, "the pipeline did not actually stop"
    assert waited < 4, "the pipeline did not resume when released"


def test_status_shows_paused_while_waiting_and_running_after(run_id):
    tracker.set_pause_requested(run_id, True)
    seen: list[str] = []

    def observe_then_release():
        time.sleep(0.15)
        seen.append(tracker.get_run_detail(run_id)["status"])
        tracker.set_pause_requested(run_id, False)

    threading.Thread(target=observe_then_release, daemon=True).start()
    pipeline._wait_while_paused(run_id, _settings())

    assert seen == ["paused"], f"status while waiting was {seen}"
    assert tracker.get_run_detail(run_id)["status"] == "running"


def test_cancelling_a_paused_run_stops_it(run_id):
    tracker.set_pause_requested(run_id, True)

    def cancel():
        time.sleep(0.1)
        tracker.finish_run(run_id, status="failed", final_summary="Cancelled manually.")

    threading.Thread(target=cancel, daemon=True).start()

    with pytest.raises(pipeline.RunCancelled):
        pipeline._wait_while_paused(run_id, _settings())


def test_a_run_left_paused_forever_is_abandoned(run_id):
    tracker.set_pause_requested(run_id, True)
    with pytest.raises(pipeline.RunCancelled, match="longer than"):
        pipeline._wait_while_paused(run_id, _settings(pause_timeout_seconds=0.05))
    # the flag is cleared so the run does not re-pause on a retry
    assert tracker.is_pause_requested(run_id) is False


def test_the_timeout_can_be_disabled(run_id):
    tracker.set_pause_requested(run_id, True)

    def release():
        time.sleep(0.2)
        tracker.set_pause_requested(run_id, False)

    threading.Thread(target=release, daemon=True).start()
    # 0 means wait indefinitely; this returns only because it was released
    pipeline._wait_while_paused(run_id, _settings(pause_timeout_seconds=0))


# --- the graph honours it ------------------------------------------------


def test_a_wrapped_node_does_not_run_while_paused(run_id):
    ran: list[str] = []
    node = pipeline.pausable(lambda state: ran.append("ran") or {}, _settings())

    tracker.set_pause_requested(run_id, True)

    def release():
        time.sleep(0.2)
        assert ran == [], "the node ran while the run was paused"
        tracker.set_pause_requested(run_id, False)

    threading.Thread(target=release, daemon=True).start()
    node({"run_id": run_id})
    assert ran == ["ran"], "the node never ran after resuming"


def test_a_wrapped_node_runs_immediately_when_not_paused(run_id):
    ran: list[str] = []
    node = pipeline.pausable(lambda state: ran.append("ran") or {}, _settings())
    node({"run_id": run_id})
    assert ran == ["ran"]


def test_a_cancelled_run_never_enters_the_node(run_id):
    ran: list[str] = []
    node = pipeline.pausable(lambda state: ran.append("ran") or {}, _settings())

    tracker.set_pause_requested(run_id, True)

    def cancel():
        time.sleep(0.1)
        tracker.finish_run(run_id, status="failed", final_summary="Cancelled manually.")

    threading.Thread(target=cancel, daemon=True).start()

    with pytest.raises(pipeline.RunCancelled):
        node({"run_id": run_id})
    assert ran == [], "a cancelled run still executed the node"


def test_every_graph_node_is_wrapped():
    """A node added without the wrapper would be a silent hole in pause."""
    import pathlib as _p
    import re

    src = _p.Path("eng_crew/pipeline.py").read_text(encoding="utf-8")
    added = re.findall(r'graph\.add_node\("([^"]+)",\s*([^)]+)\)', src)
    assert added, "no add_node calls found"
    unwrapped = [name for name, fn in added if not fn.strip().startswith("_pausable(")]
    assert not unwrapped, f"nodes missing the pause check: {unwrapped}"


def test_run_pipeline_does_not_overwrite_a_cancelled_run(db, monkeypatch):
    """Cancel already finished the run; the pipeline must leave that alone."""
    class CancellingGraph:
        def invoke(self, state):
            raise pipeline.RunCancelled("cancelled while paused")

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: CancellingGraph())

    settings = _settings()
    settings.worktree_isolation = False
    settings.commit_run_output = False

    run_id = tracker.create_run("a task", str(db))
    tracker.finish_run(run_id, status="failed", final_summary="Cancelled manually.")

    pipeline.run_pipeline(task="a task", project_path=str(db),
                          settings=settings, run_id=run_id)

    run = tracker.get_run_detail(run_id)
    assert run["final_summary"] == "Cancelled manually.", "the cancel reason was overwritten"
