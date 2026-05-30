"""
SQLite-backed run + token tracking for the eng-crew pipeline.
DB path is configured via ENG_CREW_DATA_DIR env var (default: .eng-crew/tracking.db).
"""
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .config import DATA_DIR, DB_PATH

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception as _e:
    print(f"[tracker] WARNING: could not create data dir {DATA_DIR}: {_e}", file=sys.stderr)

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    with _connect() as _conn:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_text       TEXT    NOT NULL,
                project_path    TEXT    NOT NULL,
                started_at      TEXT    NOT NULL,
                finished_at     TEXT,
                status          TEXT    NOT NULL DEFAULT 'running',
                total_subtasks  INTEGER,
                current_subtask_idx INTEGER DEFAULT 0,
                current_subtask_desc TEXT,
                total_cost_usd  REAL    NOT NULL DEFAULT 0.0,
                final_summary   TEXT
            );

            CREATE TABLE IF NOT EXISTS subtask_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL REFERENCES runs(id),
                subtask_idx     INTEGER NOT NULL,
                agent_name      TEXT    NOT NULL,
                provider        TEXT    NOT NULL,
                model           TEXT    NOT NULL,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                cost_usd        REAL    NOT NULL DEFAULT 0.0,
                timestamp       TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backlog_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                description     TEXT NOT NULL,
                project_path    TEXT NOT NULL,
                claude_md_path  TEXT NOT NULL,
                priority        INTEGER DEFAULT 50,
                status          TEXT DEFAULT 'pending',
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                sprint_id       INTEGER
            );
            CREATE TABLE IF NOT EXISTS sprint_plans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id      INTEGER,
                plan_json   TEXT NOT NULL,
                approved    INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                approved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS memories (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id        INTEGER REFERENCES runs(id),
                project_path  TEXT    NOT NULL DEFAULT '',
                date          TEXT    NOT NULL,
                title         TEXT    NOT NULL DEFAULT '',
                task          TEXT    NOT NULL DEFAULT '',
                what_worked   TEXT    NOT NULL DEFAULT '',
                what_didnt    TEXT    NOT NULL DEFAULT '',
                lesson        TEXT    NOT NULL DEFAULT '',
                files_touched TEXT    NOT NULL DEFAULT '',
                raw_text      TEXT    NOT NULL DEFAULT '',
                created_at    TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE TABLE IF NOT EXISTS projects (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                project_path    TEXT NOT NULL UNIQUE,
                claude_md_path  TEXT NOT NULL,
                repo_url        TEXT DEFAULT '',
                default_branch  TEXT DEFAULT 'main',
                tech_stack      TEXT DEFAULT '[]',
                test_command    TEXT DEFAULT 'pytest',
                description     TEXT DEFAULT '',
                active          INTEGER DEFAULT 1,
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE TABLE IF NOT EXISTS sprints (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id      INTEGER NOT NULL REFERENCES projects(id),
                issue_id        INTEGER REFERENCES backlog_items(id),
                run_id          INTEGER REFERENCES runs(id),
                name            TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'planning',
                git_branch      TEXT DEFAULT '',
                total_subtasks  INTEGER DEFAULT 0,
                done_subtasks   INTEGER DEFAULT 0,
                total_cost_usd  REAL DEFAULT 0.0,
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                started_at      TEXT,
                finished_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS project_plans (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id   INTEGER NOT NULL REFERENCES projects(id),
                goal         TEXT NOT NULL,
                status       TEXT DEFAULT 'active',
                created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                updated_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS plan_sprints (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id        INTEGER NOT NULL REFERENCES project_plans(id),
                sprint_number  INTEGER NOT NULL,
                name           TEXT NOT NULL,
                description    TEXT NOT NULL,
                rationale      TEXT DEFAULT '',
                status         TEXT DEFAULT 'pending',
                depends_on     TEXT DEFAULT '[]',
                sprint_id      INTEGER REFERENCES sprints(id),
                created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE TABLE IF NOT EXISTS partial_memories (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         INTEGER NOT NULL REFERENCES runs(id),
                subtask_id     TEXT NOT NULL,
                outcome        TEXT NOT NULL,
                lesson_hint    TEXT,
                created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );

            CREATE TABLE IF NOT EXISTS subtask_cache (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                project_path    TEXT NOT NULL,
                subtask_hash    TEXT NOT NULL,
                description     TEXT NOT NULL,
                target_files    TEXT NOT NULL,
                patch           TEXT NOT NULL,
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                UNIQUE(project_path, subtask_hash)
            );

            CREATE TABLE IF NOT EXISTS subtask_reviews (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL REFERENCES runs(id),
                subtask_id      TEXT,
                description     TEXT,
                agent_type      TEXT,
                target_files    TEXT,
                exec_summary    TEXT,
                tests_passed    INTEGER,
                clarification_question TEXT,
                clarification_response TEXT,
                clarification_options  TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                resolved_at     TEXT
            );
        """)

    # Migrations — wrapped in _lock to prevent concurrent schema changes on startup
    with _lock:
        try:
            _mig = _connect()
            se_cols = [r[1] for r in _mig.execute("PRAGMA table_info(subtask_events)").fetchall()]
            if "specialist_name" not in se_cols:
                _mig.execute("ALTER TABLE subtask_events ADD COLUMN specialist_name TEXT DEFAULT ''")
            if "result_text" not in se_cols:
                _mig.execute("ALTER TABLE subtask_events ADD COLUMN result_text TEXT DEFAULT ''")
            run_cols = [r[1] for r in _mig.execute("PRAGMA table_info(runs)").fetchall()]
            if "final_summary" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN final_summary TEXT")
            if "current_subtask_idx" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN current_subtask_idx INTEGER DEFAULT 0")
            if "current_subtask_desc" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN current_subtask_desc TEXT")
            sp_cols = [r[1] for r in _mig.execute("PRAGMA table_info(sprint_plans)").fetchall()]
            if "design_text" not in sp_cols:
                _mig.execute("ALTER TABLE sprint_plans ADD COLUMN design_text TEXT DEFAULT ''")
            if "critique_text" not in sp_cols:
                _mig.execute("ALTER TABLE sprint_plans ADD COLUMN critique_text TEXT DEFAULT ''")
            bl_cols = [r[1] for r in _mig.execute("PRAGMA table_info(backlog_items)").fetchall()]
            if "type" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN type TEXT DEFAULT 'feature'")
            if "project_id" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN project_id INTEGER REFERENCES projects(id)")
                # Back-fill project_id from project_path
                _mig.execute("""
                    UPDATE backlog_items SET project_id = (
                        SELECT id FROM projects WHERE projects.project_path = backlog_items.project_path LIMIT 1
                    ) WHERE project_id IS NULL
                """)
            if "run_id" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN run_id INTEGER REFERENCES runs(id)")
            if "subtask_id" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN subtask_id TEXT")
            if "type" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN type TEXT DEFAULT 'issue'")
            if "parent_issue_id" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN parent_issue_id INTEGER")
            if "active_sprint_id" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN active_sprint_id INTEGER")
            if "agent_type" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN agent_type TEXT DEFAULT ''")
            if "target_files" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN target_files TEXT DEFAULT ''")
            if "pause_requested" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN pause_requested INTEGER DEFAULT 0")
            ps_cols = [r[1] for r in _mig.execute("PRAGMA table_info(plan_sprints)").fetchall()]
            if "acceptance_criteria" not in ps_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN acceptance_criteria TEXT DEFAULT ''")
            if "scope_hints" not in ps_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN scope_hints TEXT DEFAULT '[]'")
            if "replans" not in sp_cols:
                _mig.execute("ALTER TABLE sprint_plans ADD COLUMN replans TEXT DEFAULT '[]'")
            if "complexity" not in ps_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN complexity TEXT DEFAULT 'medium'")
            if "risk_flags" not in ps_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN risk_flags TEXT DEFAULT '[]'")
            pp_cols = [r[1] for r in _mig.execute("PRAGMA table_info(project_plans)").fetchall()]
            if "review_result" not in pp_cols:
                _mig.execute("ALTER TABLE project_plans ADD COLUMN review_result TEXT DEFAULT ''")
            ps_run_cols = [r[1] for r in _mig.execute("PRAGMA table_info(plan_sprints)").fetchall()]
            if "run_id" not in ps_run_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN run_id INTEGER REFERENCES runs(id)")
            if "preprocessor_details" not in se_cols:
                _mig.execute("ALTER TABLE subtask_events ADD COLUMN preprocessor_details TEXT")
            if "business_summary" not in sp_cols:
                _mig.execute("ALTER TABLE sprint_plans ADD COLUMN business_summary TEXT DEFAULT ''")
            # Re-fetch ps_cols in case it was fetched before plan_sprints migration ran
            ps_cols = [r[1] for r in _mig.execute("PRAGMA table_info(plan_sprints)").fetchall()]
            if "business_summary" not in ps_cols:
                _mig.execute("ALTER TABLE plan_sprints ADD COLUMN business_summary TEXT DEFAULT ''")
            run_cols = [r[1] for r in _mig.execute("PRAGMA table_info(runs)").fetchall()]
            if "hitl_decision" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN hitl_decision TEXT DEFAULT NULL")
            bl_cols = [r[1] for r in _mig.execute("PRAGMA table_info(backlog_items)").fetchall()]
            if "diff_text" not in bl_cols:
                _mig.execute("ALTER TABLE backlog_items ADD COLUMN diff_text TEXT DEFAULT ''")
            sr_cols = [r[1] for r in _mig.execute("PRAGMA table_info(subtask_reviews)").fetchall()]
            if "clarification_question" not in sr_cols:
                _mig.execute("ALTER TABLE subtask_reviews ADD COLUMN clarification_question TEXT")
            if "clarification_response" not in sr_cols:
                _mig.execute("ALTER TABLE subtask_reviews ADD COLUMN clarification_response TEXT")
            if "clarification_options" not in sr_cols:
                _mig.execute("ALTER TABLE subtask_reviews ADD COLUMN clarification_options TEXT")
            if "log_path" not in run_cols:
                _mig.execute("ALTER TABLE runs ADD COLUMN log_path TEXT")
            _mig.commit()
            _mig.close()
        except Exception as e:
            print(f"[tracker] MIGRATION WARNING: {e}", file=sys.stderr)


def cleanup_stale_backlog_items() -> int:
    """Reset backlog items stuck in 'running' whose associated run has finished.
    Items linked to a completed run become 'done'; failed/aborted runs become 'failed'.
    Items with no run_id are reset to 'pending'.
    Returns the number of items fixed."""
    try:
        with _lock:
            with _connect() as conn:
                # Items whose run finished as completed/rejected → done
                cur = conn.execute(
                    """UPDATE backlog_items
                       SET status='done'
                       WHERE status='running'
                         AND run_id IS NOT NULL
                         AND run_id IN (
                             SELECT id FROM runs WHERE status IN ('completed', 'rejected')
                         )"""
                )
                done_count = cur.rowcount

                # Items whose run finished as failed/aborted → failed
                cur = conn.execute(
                    """UPDATE backlog_items
                       SET status='failed'
                       WHERE status='running'
                         AND run_id IS NOT NULL
                         AND run_id IN (
                             SELECT id FROM runs WHERE status IN ('failed', 'aborted')
                         )"""
                )
                failed_count = cur.rowcount

                # Items with no run_id stuck in running → reset to pending
                cur = conn.execute(
                    "UPDATE backlog_items SET status='pending' WHERE status='running' AND run_id IS NULL"
                )
                reset_count = cur.rowcount

                # Pending items on finished runs (subtasks the executor never reached)
                cur = conn.execute(
                    """UPDATE backlog_items SET status='done'
                       WHERE status='pending' AND run_id IS NOT NULL
                         AND run_id IN (SELECT id FROM runs WHERE status='completed')"""
                )
                done_count += cur.rowcount
                cur = conn.execute(
                    """UPDATE backlog_items SET status='failed'
                       WHERE status='pending' AND run_id IS NOT NULL
                         AND run_id IN (SELECT id FROM runs WHERE status IN ('failed','rejected','aborted'))"""
                )
                failed_count += cur.rowcount

                total = done_count + failed_count + reset_count
                if total:
                    print(
                        f"[tracker] cleanup_stale_backlog_items: fixed {total} stuck item(s) "
                        f"(done={done_count}, failed={failed_count}, reset={reset_count})",
                        file=sys.stderr,
                    )
                return total
    except Exception as e:
        print(f"[tracker] cleanup_stale_backlog_items error: {e}", file=sys.stderr)
        return 0


def cleanup_stale_runs(max_age_hours: int = 4) -> int:
    """Mark runs stuck in 'running'/'awaiting_approval' for too long as 'failed'.
    Returns the number of runs cleaned up. Called at dashboard startup."""
    try:
        with _lock:
            with _connect() as conn:
                cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                # SQLite datetime arithmetic: subtract max_age_hours hours
                cur = conn.execute(
                    """UPDATE runs
                       SET status='failed', finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                       WHERE status IN ('running', 'awaiting_approval')
                         AND started_at < datetime('now', ?)""",
                    (f"-{max_age_hours} hours",),
                )
                count = cur.rowcount
                if count:
                    print(f"[tracker] cleanup_stale_runs: marked {count} stale run(s) as failed", file=sys.stderr)
                return count
    except Exception as e:
        print(f"[tracker] cleanup_stale_runs error: {e}", file=sys.stderr)
        return 0


_init_db()
cleanup_stale_runs()
cleanup_stale_backlog_items()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_run(task_text: str, project_path: str, log_path: str | None = None) -> int:
    """Insert a new run record and return its integer ID."""
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (task_text, project_path, started_at, log_path) VALUES (?, ?, ?, ?)",
                (task_text, project_path, _now(), log_path),
            )
            return cur.lastrowid


def update_run_log_path(run_id: int, log_path: str) -> None:
    """Update the log path for an existing run."""
    with _lock:
        with _connect() as conn:
            conn.execute("UPDATE runs SET log_path=? WHERE id=?", (log_path, run_id))


def update_run_subtask_count(run_id: int, total: int):
    """Called by architect after decomposition so the dashboard can show a progress bar."""
    try:
        with _lock:
            with _connect() as conn:
                conn.execute(
                    "UPDATE runs SET total_subtasks=? WHERE id=?",
                    (total, run_id),
                )
    except Exception as e:
        print(f"[tracker] update_run_subtask_count error: {e}", file=sys.stderr)


def finish_run(run_id: int, status: str = "completed", final_summary: str = "") -> None:
    """Mark run complete, recalculate total cost, and store final summary."""
    try:
        with _lock:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) FROM subtask_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                total_cost = row[0] if row else 0.0
                conn.execute(
                    "UPDATE runs SET finished_at=?, status=?, total_cost_usd=?, final_summary=? WHERE id=?",
                    (_now(), status, total_cost, final_summary or None, run_id),
                )
    except Exception as e:
        print(f"[tracker] finish_run error: {e}", file=sys.stderr)


def update_run_status(run_id: int, status: str) -> None:
    try:
        with _lock:
            with _connect() as conn:
                conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
    except Exception as e:
        print(f"[tracker] update_run_status error: {e}", file=sys.stderr)


def update_run_current_subtask(run_id: int, subtask_idx: int, description: str) -> None:
    """Update the current subtask index and description for a run."""
    try:
        with _lock:
            with _connect() as conn:
                conn.execute(
                    "UPDATE runs SET current_subtask_idx=?, current_subtask_desc=? WHERE id=?",
                    (subtask_idx, description[:200], run_id),
                )
    except Exception as e:
        print(f"[tracker] update_run_current_subtask error: {e}", file=sys.stderr)


def set_pause_requested(run_id: int, requested: bool) -> None:
    try:
        with _lock:
            with _connect() as conn:
                conn.execute("UPDATE runs SET pause_requested=? WHERE id=?",
                             (1 if requested else 0, run_id))
    except Exception as e:
        print(f"[tracker] set_pause_requested error: {e}", file=sys.stderr)


def is_pause_requested(run_id: int) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT pause_requested FROM runs WHERE id=?", (run_id,)).fetchone()
            return bool(row and row[0])
    except Exception:
        return False


_MAX_RESULT_TEXT = 8000  # chars stored per event — enough to show what changed


def log_event(run_id: int, subtask_idx: int, agent_name: str, llm_result,
              specialist_name: str = "") -> None:
    """
    Record a single LLM call. llm_result is a providers.base.LLMResult instance.
    Never raises — tracking failure must not crash the pipeline.
    """
    try:
        result_text = (llm_result.text or "")[:_MAX_RESULT_TEXT]
        with _lock:
            with _connect() as conn:
                conn.execute(
                    """INSERT INTO subtask_events
                       (run_id, subtask_idx, agent_name, provider, model,
                        input_tokens, output_tokens, cost_usd, timestamp, specialist_name,
                        result_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        subtask_idx,
                        agent_name,
                        llm_result.provider,
                        llm_result.model,
                        llm_result.input_tokens,
                        llm_result.output_tokens,
                        llm_result.cost_usd,
                        _now(),
                        specialist_name,
                        result_text,
                    ),
                )
    except Exception as e:
        print(f"[tracker] log_event error: {e}", file=sys.stderr)


def log_subtask_event(
    run_id: int,
    subtask_idx: int,
    event_type: str,
    details: dict = None,
    preprocessor_details: dict = None,
) -> None:
    """
    Log a named event for a subtask (e.g. 'preprocessor_start', 'preprocessor_done').
    Stores optional structured details and preprocessor_details as JSON blobs.
    Never raises — tracking failure must not crash the pipeline.
    """
    try:
        details_json = json.dumps(details) if details else None
        preprocessor_details_json = json.dumps(preprocessor_details) if preprocessor_details else None
        with _lock:
            with _connect() as conn:
                conn.execute(
                    """INSERT INTO subtask_events
                       (run_id, subtask_idx, agent_name, provider, model,
                        input_tokens, output_tokens, cost_usd, timestamp,
                        result_text, preprocessor_details)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id,
                        subtask_idx,
                        event_type,
                        "",
                        "",
                        0,
                        0,
                        0.0,
                        _now(),
                        details_json,
                        preprocessor_details_json,
                    ),
                )
    except Exception as e:
        print(f"[tracker] log_subtask_event error: {e}", file=sys.stderr)


def record_preprocessor_event(
    run_id: int, subtask_idx: int, gate: str, before_chars: int, after_chars: int
) -> None:
    """Insert a zero-cost preprocessor gate event into run_events for dashboard display."""
    try:
        result_text = f"{before_chars:,} → {after_chars:,} chars"
        with _lock:
            with _connect() as conn:
                conn.execute(
                    """INSERT INTO subtask_events
                       (run_id, subtask_idx, agent_name, provider, model,
                        input_tokens, output_tokens, cost_usd, timestamp,
                        specialist_name, result_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run_id, subtask_idx, "preprocessor", "", "",
                        0, 0, 0.0, _now(), gate, result_text,
                    ),
                )
    except Exception as e:
        print(f"[tracker] record_preprocessor_event error: {e}", file=sys.stderr)


# ── Dashboard query helpers ──────────────────────────────────────────────────

def get_active_runs_for_project(project_path: str) -> list[dict]:
    """Return active (running/awaiting_approval/paused) runs for a given project path."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, task_text, status FROM runs WHERE project_path=? AND status IN ('running', 'awaiting_approval', 'paused')",
                (project_path,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_active_runs_for_project error: {e}", file=sys.stderr)
        return []


def find_duplicate_run(task_text: str, project_path: str) -> dict | None:
    """Return a running/pending run with the same task_text+project, if any."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, status FROM runs WHERE task_text=? AND project_path=? AND status IN ('running', 'awaiting_approval', 'paused') ORDER BY id DESC LIMIT 1",
                (task_text, project_path),
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[tracker] find_duplicate_run error: {e}", file=sys.stderr)
        return None


def get_run_cost(run_id: int) -> float:
    """Return the total cost so far for a specific run."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM subtask_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0
    except Exception:
        return 0.0


def get_active_runs() -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT r.id, r.task_text, r.project_path, r.started_at,
                       r.status,
                       r.total_subtasks,
                       r.current_subtask_idx,
                       r.current_subtask_desc,
                       (SELECT COUNT(DISTINCT subtask_idx) FROM subtask_events
                        WHERE run_id=r.id AND subtask_idx >= 0) AS done_subtasks,
                       (SELECT agent_name FROM subtask_events
                        WHERE run_id=r.id ORDER BY id DESC LIMIT 1) AS current_agent,
                       (SELECT COALESCE(SUM(cost_usd), 0) FROM subtask_events
                        WHERE run_id=r.id) AS running_cost
                FROM runs r WHERE r.status IN ('running', 'awaiting_approval', 'paused', 'awaiting_clarification')
                ORDER BY r.id DESC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_active_runs error: {e}", file=sys.stderr)
        return []


def get_runs_by_status(status: str) -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, task_text, project_path, status FROM runs WHERE status = ? ORDER BY id DESC",
                (status,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_runs_by_status error: {e}", file=sys.stderr)
        return []


def set_hitl_decision(run_id: int, decision: dict) -> None:
    """Write HITL decision to DB so pipeline processes in other PIDs can read it."""
    try:
        with _lock:
            with _connect() as conn:
                conn.execute(
                    "UPDATE runs SET hitl_decision = ? WHERE id = ?",
                    (json.dumps(decision), run_id)
                )
                conn.commit()
    except Exception as e:
        print(f"[tracker] set_hitl_decision error: {e}", file=sys.stderr)


def get_hitl_decision(run_id: int) -> dict | None:
    """Read HITL decision from DB. Returns None if not yet set."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT hitl_decision FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row and row[0]:
                return json.loads(row[0])
    except Exception as e:
        print(f"[tracker] get_hitl_decision error: {e}", file=sys.stderr)
    return None


def clear_hitl_decision(run_id: int) -> None:
    """Clear HITL decision after it has been consumed."""
    try:
        with _lock:
            with _connect() as conn:
                conn.execute("UPDATE runs SET hitl_decision = NULL WHERE id = ?", (run_id,))
                conn.commit()
    except Exception as e:
        print(f"[tracker] clear_hitl_decision error: {e}", file=sys.stderr)


def get_recent_runs(limit: int = 10) -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT id, task_text, project_path, started_at, finished_at,
                       status, total_subtasks, total_cost_usd
                FROM runs WHERE status != 'running'
                ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_recent_runs error: {e}", file=sys.stderr)
        return []


def get_run_events(run_id: int) -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT id, subtask_idx, agent_name, specialist_name, provider, model,
                       input_tokens, output_tokens, cost_usd, timestamp, result_text
                FROM subtask_events WHERE run_id=?
                ORDER BY id ASC
            """, (run_id,)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_run_events error: {e}", file=sys.stderr)
        return []


def get_run_detail(run_id: int) -> dict | None:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[tracker] get_run_detail error: {e}", file=sys.stderr)
        return None


def get_project_runs(project_path: str, limit: int = 50) -> list[dict]:
    """All runs for a given project path, newest first, with per-run cost."""
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT r.id, r.task_text, r.status, r.started_at, r.finished_at,
                       r.total_subtasks,
                       COALESCE(r.total_cost_usd,
                           (SELECT COALESCE(SUM(cost_usd),0) FROM subtask_events WHERE run_id=r.id)
                       ) AS cost_usd,
                       (SELECT COUNT(DISTINCT subtask_idx) FROM subtask_events
                        WHERE run_id=r.id AND subtask_idx >= 0) AS done_subtasks
                FROM runs r
                WHERE r.project_path = ?
                ORDER BY r.id DESC LIMIT ?
            """, (project_path, limit)).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_project_runs error: {e}", file=sys.stderr)
        return []


def get_project_stats(project_path: str) -> dict:
    """Aggregate stats for a project: run counts by status + total cost."""
    try:
        with _connect() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status='failed'    THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status='running'   THEN 1 ELSE 0 END) AS running,
                    COALESCE(SUM(total_cost_usd), 0) AS total_cost_usd
                FROM runs WHERE project_path = ?
            """, (project_path,)).fetchone()
            return dict(row) if row else {}
    except Exception as e:
        print(f"[tracker] get_project_stats error: {e}", file=sys.stderr)
        return {}


def get_claude_usage_stats() -> dict:
    """
    Queries subtask_events for Claude CLI usage statistics within two windows:
    - Last 24 hours (session)
    - Current week (Monday 00:00:00 UTC to now)

    Returns:
        dict: {session_cost: float, session_count: int, week_cost: float, week_count: int}
    """
    with _lock:
        with _connect() as conn:
            # Calculate 24 hours ago
            session_start_time_str = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')"
            ).fetchone()[0]

            # Calculate start of current week (Monday 00:00:00 UTC)
            # strftime('%w', 'now') gives 0 for Sunday, 1 for Monday, ..., 6 for Saturday.
            # We want to subtract (weekday_num - 1 + 7) % 7 days to get to Monday.
            # E.g., if today is Monday (1), subtract 0 days.
            # If today is Sunday (0), subtract 6 days.
            week_start_time_str = conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', 'start of day', '-' || ((CAST(strftime('%w', 'now') AS INTEGER) - 1 + 7) % 7) || ' days')"
            ).fetchone()[0]

            # The earliest timestamp to filter by for the main WHERE clause
            # is the week_start_time_str, as it's always <= session_start_time_str
            earliest_filter_time = week_start_time_str

            query = """
                SELECT
                    SUM(CASE WHEN timestamp >= ? THEN cost_usd ELSE 0 END) AS session_cost,
                    COUNT(CASE WHEN timestamp >= ? THEN 1 ELSE NULL END) AS session_count,
                    SUM(CASE WHEN timestamp >= ? THEN cost_usd ELSE 0 END) AS week_cost,
                    COUNT(CASE WHEN timestamp >= ? THEN 1 ELSE NULL END) AS week_count
                FROM
                    subtask_events
                WHERE
                    provider = 'claude_cli'
                    AND timestamp >= ?;
            """

            params = (
                session_start_time_str,
                session_start_time_str,
                week_start_time_str,
                week_start_time_str,
                earliest_filter_time,
            )

            row = conn.execute(query, params).fetchone()

            if row:
                return {
                    "session_cost": row["session_cost"] or 0.0,
                    "session_count": row["session_count"] or 0,
                    "week_cost": row["week_cost"] or 0.0,
                    "week_count": row["week_count"] or 0,
                }
            else:
                return {
                    "session_cost": 0.0,
                    "session_count": 0,
                    "week_cost": 0.0,
                    "week_count": 0,
                }


def get_cost_by_model() -> list[dict]:
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT provider, model,
                       SUM(input_tokens)  AS total_input_tokens,
                       SUM(output_tokens) AS total_output_tokens,
                       SUM(cost_usd)      AS total_cost_usd,
                       COUNT(*)           AS call_count
                FROM subtask_events
                GROUP BY provider, model
                ORDER BY total_cost_usd DESC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_cost_by_model error: {e}", file=sys.stderr)
        return []


def get_run_usage(run_id: int) -> dict:
    """Return token + cost totals for a single run, broken down by provider."""
    try:
        with _connect() as conn:
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens),  0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cost_usd),      0) AS cost_usd,
                    COUNT(*)                        AS call_count
                FROM subtask_events WHERE run_id=?
            """, (run_id,)).fetchone()
            totals = dict(row) if row else {}

            rows = conn.execute("""
                SELECT provider,
                       COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM subtask_events WHERE run_id=?
                GROUP BY provider ORDER BY tokens DESC
            """, (run_id,)).fetchall()
            totals["by_provider"] = [dict(r) for r in rows]
            return totals
    except Exception as e:
        print(f"[tracker] get_run_usage error: {e}", file=sys.stderr)
        return {}


def get_weekly_usage(days: int = 7) -> dict:
    """Return token + cost totals for the last N days, broken down by provider."""
    try:
        with _connect() as conn:
            cutoff = f"-{days} days"
            row = conn.execute("""
                SELECT
                    COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens),  0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cost_usd),      0) AS cost_usd,
                    COUNT(DISTINCT run_id)           AS run_count,
                    COUNT(*)                        AS call_count
                FROM subtask_events
                WHERE timestamp >= datetime('now', ?)
            """, (cutoff,)).fetchone()
            totals = dict(row) if row else {}

            rows = conn.execute("""
                SELECT provider,
                       COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                       COALESCE(SUM(input_tokens),  0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cost_usd), 0) AS cost_usd
                FROM subtask_events
                WHERE timestamp >= datetime('now', ?)
                GROUP BY provider ORDER BY tokens DESC
            """, (cutoff,)).fetchall()
            totals["by_provider"] = [dict(r) for r in rows]
            totals["days"] = days
            return totals
    except Exception as e:
        print(f"[tracker] get_weekly_usage error: {e}", file=sys.stderr)
        return {}


# ── Backlog CRUD ──────────────────────────────────────────────────────────────

def add_backlog_item(title: str, description: str, project_path: str,
                     claude_md_path: str, priority: int = 50,
                     project_id: int = None, item_type: str = 'feature') -> int:
    with _lock:
        with _connect() as con:
            cur = con.execute(
                "INSERT INTO backlog_items (title, description, project_path, claude_md_path, priority, project_id, type) VALUES (?,?,?,?,?,?,?)",
                (title, description, project_path, claude_md_path, priority, project_id, item_type),
            )
            return cur.lastrowid


def list_backlog_items(status: str = None, project_path: str = None,
                       type: str = None) -> list:
    with _lock:
        with _connect() as con:
            query = "SELECT * FROM backlog_items WHERE 1=1"
            params = []
            if status:
                query += " AND status=?"; params.append(status)
            if project_path:
                query += " AND project_path=?"; params.append(project_path)
            if type:
                query += " AND type=?"; params.append(type)
            query += " ORDER BY priority ASC, created_at ASC"
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]


def update_backlog_item(item_id: int, **fields) -> None:
    if not fields:
        return
    with _lock:
        with _connect() as con:
            set_clause = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE backlog_items SET {set_clause} WHERE id=?", [*fields.values(), item_id])


def delete_backlog_item(item_id: int) -> None:
    with _lock:
        with _connect() as con:
            con.execute("DELETE FROM backlog_items WHERE id=?", (item_id,))


def get_backlog_item(item_id: int) -> dict | None:
    con = _connect()
    row = con.execute("SELECT * FROM backlog_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


def list_issues(project_id: int, status: str | None = None) -> list[dict]:
    """Return all type='issue' backlog items for a project."""
    con = _connect()
    query = "SELECT * FROM backlog_items WHERE project_id=? AND type='issue'"
    params: list = [project_id]
    if status:
        query += " AND status=?"; params.append(status)
    query += " ORDER BY priority ASC, created_at ASC"
    rows = con.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ── Sprints ───────────────────────────────────────────────────────────────────

def create_sprint(project_id: int, issue_id: int | None, name: str = "") -> int:
    with _lock:
        with _connect() as con:
            cur = con.execute(
                """INSERT INTO sprints (project_id, issue_id, name, status)
                   VALUES (?, ?, ?, 'planning')""",
                (project_id, issue_id, name),
            )
            return cur.lastrowid


def update_sprint(sprint_id: int, **fields) -> None:
    if not fields:
        return
    try:
        with _lock:
            with _connect() as con:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                con.execute(
                    f"UPDATE sprints SET {set_clause} WHERE id=?",
                    [*fields.values(), sprint_id],
                )
    except Exception as e:
        print(f"[tracker] update_sprint error: {e}", file=sys.stderr)


def finish_sprint(sprint_id: int, status: str, total_cost_usd: float = 0.0) -> None:
    try:
        with _lock:
            with _connect() as con:
                con.execute(
                    """UPDATE sprints
                       SET status=?, total_cost_usd=?,
                           finished_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                       WHERE id=?""",
                    (status, total_cost_usd, sprint_id),
                )
    except Exception as e:
        print(f"[tracker] finish_sprint error: {e}", file=sys.stderr)


def get_sprint(sprint_id: int) -> dict | None:
    con = _connect()
    row = con.execute("SELECT * FROM sprints WHERE id=?", (sprint_id,)).fetchone()
    return dict(row) if row else None


def get_project_sprints(project_id: int, limit: int = 20) -> list[dict]:
    con = _connect()
    rows = con.execute(
        "SELECT * FROM sprints WHERE project_id=? ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_sprint_subtasks(sprint_id: int) -> list[dict]:
    con = _connect()
    rows = con.execute(
        "SELECT * FROM backlog_items WHERE sprint_id=? AND type='subtask' ORDER BY id ASC",
        (sprint_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_subtask_backlog_items(
    run_id: int,
    subtasks: list,
    project_path: str,
    claude_md_path: str,
    project_id: int | None,
    sprint_id: int | None = None,
    parent_issue_id: int | None = None,
) -> None:
    """Save each approved subtask as a backlog item so the project task view shows them."""
    try:
        with _lock:
            with _connect() as con:
                for st in subtasks:
                    # Skip if this subtask was already recorded for this run
                    already = con.execute(
                        "SELECT 1 FROM backlog_items WHERE run_id=? AND subtask_id=?",
                        (run_id, str(st.get("id", ""))),
                    ).fetchone()
                    if already:
                        continue
                    files = ", ".join(st.get("target_files") or []) or "—"
                    description = (
                        f"[Run #{run_id}] agent: {st.get('agent_type', '?')} | "
                        f"files: {files}"
                    )
                    con.execute(
                        """INSERT INTO backlog_items
                           (title, description, project_path, claude_md_path,
                            priority, status, project_id, run_id, subtask_id,
                            type, sprint_id, parent_issue_id, agent_type, target_files)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            st.get("description", "")[:200],
                            description,
                            project_path,
                            claude_md_path,
                            50,
                            "pending",
                            project_id,
                            run_id,
                            str(st.get("id", "")),
                            "subtask",
                            sprint_id,
                            parent_issue_id,
                            st.get("agent_type", ""),
                            ",".join(st.get("target_files") or []),
                        ),
                    )
    except Exception as e:
        print(f"[tracker] create_subtask_backlog_items error: {e}", file=sys.stderr)


def update_subtask_item_status(run_id: int, subtask_id: str, status: str) -> None:
    """Update the backlog item status for a specific subtask within a run."""
    try:
        with _lock:
            with _connect() as con:
                con.execute(
                    "UPDATE backlog_items SET status=? WHERE run_id=? AND subtask_id=?",
                    (status, run_id, str(subtask_id)),
                )
    except Exception as e:
        print(f"[tracker] update_subtask_item_status error: {e}", file=sys.stderr)


def get_run_subtask_statuses(run_id: int) -> dict[str, str]:
    """Return {subtask_id: status} for all backlog items belonging to a run."""
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT subtask_id, status FROM backlog_items WHERE run_id=? AND subtask_id IS NOT NULL",
                (run_id,),
            ).fetchall()
        return {r["subtask_id"]: r["status"] for r in rows}
    except Exception:
        return {}


def get_project_task_summary() -> list[dict]:
    """Returns per-project task counts grouped by status."""
    try:
        with _connect() as conn:
            rows = conn.execute("""
                SELECT p.id, p.name, p.project_path,
                       COUNT(CASE WHEN b.status='pending' THEN 1 END) AS pending,
                       COUNT(CASE WHEN b.status='running' THEN 1 END) AS running,
                       COUNT(CASE WHEN b.status='done'    THEN 1 END) AS done,
                       COUNT(CASE WHEN b.status='failed'  THEN 1 END) AS failed,
                       COUNT(b.id) AS total
                FROM projects p
                LEFT JOIN backlog_items b ON b.project_path = p.project_path
                WHERE p.active = 1
                GROUP BY p.id
                ORDER BY p.name ASC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_project_task_summary error: {e}", file=sys.stderr)
        return []


# ── Sprint Plans ──────────────────────────────────────────────────────────────

def create_sprint_plan(run_id: int, plan_json: str, *,
                       design_text: str = "", critique_text: str = "",
                       business_summary: str = "") -> int:
    con = _connect()
    cur = con.execute(
        "INSERT INTO sprint_plans (run_id, plan_json, design_text, critique_text, business_summary) VALUES (?,?,?,?,?)",
        (run_id, plan_json, design_text, critique_text, business_summary),
    )
    con.commit()
    return cur.lastrowid


def approve_sprint_plan(plan_id: int) -> None:
    con = _connect()
    con.execute(
        "UPDATE sprint_plans SET approved=1, approved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        (plan_id,),
    )
    con.commit()


def append_sprint_plan_replan(run_id: int, replan_entry: dict) -> None:
    """Append a replan entry to sprint_plans.replans for audit trail."""
    con = _connect()
    row = con.execute(
        "SELECT id, replans FROM sprint_plans WHERE run_id=? ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if not row:
        con.close()
        return
    replans = json.loads(row["replans"] or "[]")
    replans.append(replan_entry)
    con.execute("UPDATE sprint_plans SET replans=? WHERE id=?", (json.dumps(replans), row["id"]))
    con.commit()
    con.close()


def get_sprint_plan(run_id: int) -> dict | None:
    con = _connect()
    row = con.execute(
        "SELECT * FROM sprint_plans WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)
    ).fetchone()
    if not row:
        return None
    return dict(row)


# ── Projects registry ─────────────────────────────────────────────────────────

def _project_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["tech_stack"] = json.loads(d.get("tech_stack") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tech_stack"] = []
    return d


def add_project(name: str, project_path: str, claude_md_path: str,
                repo_url: str = "", default_branch: str = "main",
                tech_stack: list = None, test_command: str = "pytest",
                description: str = "") -> int:
    con = _connect()
    cur = con.execute(
        """INSERT INTO projects
           (name, project_path, claude_md_path, repo_url, default_branch,
            tech_stack, test_command, description)
           VALUES (?,?,?,?,?,?,?,?)""",
        (name, project_path, claude_md_path, repo_url, default_branch,
         json.dumps(tech_stack or []), test_command, description),
    )
    con.commit()
    return cur.lastrowid


def list_projects(active_only: bool = True) -> list:
    con = _connect()
    query = "SELECT * FROM projects"
    if active_only:
        query += " WHERE active=1"
    query += " ORDER BY name ASC"
    return [_project_row(r) for r in con.execute(query).fetchall()]


def get_project(project_id: int) -> dict | None:
    con = _connect()
    row = con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    return _project_row(row) if row else None


def get_project_by_path(project_path: str) -> dict | None:
    con = _connect()
    row = con.execute("SELECT * FROM projects WHERE project_path=?", (project_path,)).fetchone()
    return _project_row(row) if row else None


def update_project(project_id: int, **fields) -> None:
    if not fields:
        return
    if "tech_stack" in fields and isinstance(fields["tech_stack"], list):
        fields["tech_stack"] = json.dumps(fields["tech_stack"])
    con = _connect()
    set_clause = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE projects SET {set_clause} WHERE id=?", [*fields.values(), project_id])
    con.commit()


def delete_project(project_id: int) -> None:
    con = _connect()
    con.execute("DELETE FROM projects WHERE id=?", (project_id,))
    con.commit()


# ── Team Memory ───────────────────────────────────────────────────────────────

def add_memory(
    run_id: int,
    project_path: str,
    date: str,
    title: str,
    task: str,
    what_worked: str,
    what_didnt: str,
    lesson: str,
    files_touched: str,
    raw_text: str,
) -> int:
    """Insert a structured memory lesson and return its ID."""
    with _lock:
        with _connect() as con:
            cur = con.execute(
                """INSERT INTO memories
                   (run_id, project_path, date, title, task, what_worked,
                    what_didnt, lesson, files_touched, raw_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, project_path, date, title, task, what_worked,
                 what_didnt, lesson, files_touched, raw_text),
            )
            return cur.lastrowid


def clear_project_tasks(project_id: int) -> int:
    """Delete all backlog_items and sprints for a project. Returns count deleted."""
    try:
        with _lock:
            with _connect() as conn:
                p = conn.execute("SELECT project_path FROM projects WHERE id=?", (project_id,)).fetchone()
                if not p:
                    return 0
                project_path = p[0]
                cur1 = conn.execute("DELETE FROM backlog_items WHERE project_path=?", (project_path,))
                cur2 = conn.execute("DELETE FROM sprints WHERE project_id=?", (project_id,))
                total = cur1.rowcount + cur2.rowcount
                print(f"[tracker] clear_project_tasks: deleted {total} records for project {project_id}", file=sys.stderr)
                return total
    except Exception as e:
        print(f"[tracker] clear_project_tasks error: {e}", file=sys.stderr)
        return 0


def clear_all_tasks() -> dict:
    """Delete ALL backlog_items, sprints, project_plans, plan_sprints. Returns counts."""
    try:
        with _lock:
            with _connect() as conn:
                c1 = conn.execute("DELETE FROM plan_sprints").rowcount
                c2 = conn.execute("DELETE FROM project_plans").rowcount
                c3 = conn.execute("DELETE FROM backlog_items").rowcount
                c4 = conn.execute("DELETE FROM sprints").rowcount
                counts = {"plan_sprints": c1, "project_plans": c2, "backlog_items": c3, "sprints": c4}
                print(f"[tracker] clear_all_tasks: {counts}", file=sys.stderr)
                return counts
    except Exception as e:
        print(f"[tracker] clear_all_tasks error: {e}", file=sys.stderr)
        return {}


# ── Project Plans (Scrum Master) ──────────────────────────────────────────────

def create_project_plan(project_id: int, goal: str) -> int:
    with _lock:
        with _connect() as conn:
            # If an active plan with sprints already exists, return it instead of creating a new one
            existing = conn.execute(
                "SELECT id FROM project_plans WHERE project_id=? AND status='active' ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if existing:
                sprint_count = conn.execute(
                    "SELECT COUNT(*) FROM plan_sprints WHERE plan_id=?",
                    (existing["id"],),
                ).fetchone()[0]
                if sprint_count > 0:
                    return existing["id"]
            cur = conn.execute(
                "INSERT INTO project_plans (project_id, goal) VALUES (?, ?)",
                (project_id, goal),
            )
            # Archive any previously active plan (only reached when previous plan has zero sprints)
            conn.execute(
                "UPDATE project_plans SET status='archived' WHERE project_id=? AND id!=? AND status='active'",
                (project_id, cur.lastrowid),
            )
            return cur.lastrowid


def get_project_plan(plan_id: int) -> dict | None:
    con = _connect()
    row = con.execute("SELECT * FROM project_plans WHERE id=?", (plan_id,)).fetchone()
    return dict(row) if row else None


def get_active_plan(project_id: int) -> dict | None:
    con = _connect()
    row = con.execute(
        "SELECT * FROM project_plans WHERE project_id=? AND status='active' ORDER BY id DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def update_project_plan(plan_id: int, **fields) -> None:
    if not fields:
        return
    try:
        with _lock:
            with _connect() as conn:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE project_plans SET {set_clause} WHERE id=?",
                    [*fields.values(), plan_id],
                )
    except Exception as e:
        print(f"[tracker] update_project_plan error: {e}", file=sys.stderr)


def add_plan_sprint(
    plan_id: int,
    sprint_number: int,
    name: str,
    description: str,
    rationale: str = "",
    depends_on: list | None = None,
    acceptance_criteria: str = "",
    scope_hints: list | None = None,
    complexity: str = "medium",
    risk_flags: list | None = None,
    business_summary: str = "",
) -> int:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT INTO plan_sprints
                   (plan_id, sprint_number, name, description, rationale, depends_on,
                    acceptance_criteria, scope_hints, complexity, risk_flags, business_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (plan_id, sprint_number, name, description, rationale,
                 json.dumps(depends_on or []),
                 acceptance_criteria,
                 json.dumps(scope_hints or []),
                 complexity,
                 json.dumps(risk_flags or []),
                 business_summary),
            )
            return cur.lastrowid


def update_plan_sprint(plan_sprint_id: int, **fields) -> None:
    if not fields:
        return
    try:
        with _lock:
            with _connect() as conn:
                set_clause = ", ".join(f"{k}=?" for k in fields)
                conn.execute(
                    f"UPDATE plan_sprints SET {set_clause} WHERE id=?",
                    [*fields.values(), plan_sprint_id],
                )
    except Exception as e:
        print(f"[tracker] update_plan_sprint error: {e}", file=sys.stderr)


def get_plan_sprints(plan_id: int) -> list[dict]:
    con = _connect()
    rows = con.execute(
        "SELECT * FROM plan_sprints WHERE plan_id=? ORDER BY sprint_number ASC",
        (plan_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for field in ("depends_on", "scope_hints", "risk_flags"):
            try:
                d[field] = json.loads(d.get(field) or "[]")
            except (json.JSONDecodeError, TypeError):
                d[field] = []
        # If sprint is still marked 'running' but its run has finished, sync the status.
        # Use task completion as the source of truth: if all tasks are done the sprint is
        # done regardless of the run's status string (which can be "failed" due to
        # failure keywords in the summary even when all code changes were applied).
        if d.get("status") == "running" and d.get("run_id"):
            run_row = con.execute(
                "SELECT status FROM runs WHERE id=?", (d["run_id"],)
            ).fetchone()
            if run_row and run_row["status"] in ("completed", "failed", "rejected"):
                # Check actual task outcomes
                task_rows = con.execute(
                    "SELECT status FROM backlog_items WHERE run_id=? AND subtask_id IS NOT NULL",
                    (d["run_id"],),
                ).fetchall()
                if task_rows:
                    all_done = all(r["status"] in ("done", "completed") for r in task_rows)
                    any_failed = any(r["status"] == "failed" for r in task_rows)
                    new_status = "done" if all_done else ("failed" if any_failed else "done")
                else:
                    # No task rows yet — fall back to run status
                    new_status = "done" if run_row["status"] == "completed" else "failed"
                try:
                    with _lock:
                        with _connect() as wcon:
                            wcon.execute(
                                "UPDATE plan_sprints SET status=? WHERE id=?",
                                (new_status, d["id"]),
                            )
                except Exception:
                    pass
                d["status"] = new_status
        result.append(d)
    return result


def get_plan_sprint_ready(plan_sprint_id: int) -> tuple[bool, list[str]]:
    """Returns (is_ready, [blocking_sprint_names]).
    Checks that all sprints listed in this sprint's depends_on are status='done' or 'skipped'.
    """
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT plan_id, depends_on FROM plan_sprints WHERE id=?",
                (plan_sprint_id,),
            ).fetchone()
            if not row:
                return True, []
            plan_id = row["plan_id"]
            try:
                depends_on = json.loads(row["depends_on"] or "[]")
            except (json.JSONDecodeError, TypeError):
                depends_on = []
            if not depends_on:
                return True, []
            blockers = []
            for sprint_number in depends_on:
                dep = con.execute(
                    "SELECT name, status FROM plan_sprints WHERE plan_id=? AND sprint_number=?",
                    (plan_id, sprint_number),
                ).fetchone()
                if dep is None or dep["status"] not in ("done", "skipped"):
                    name = dep["name"] if dep else f"Sprint {sprint_number}"
                    blockers.append(name)
            return (len(blockers) == 0), blockers
    except Exception as e:
        print(f"[tracker] get_plan_sprint_ready error: {e}", file=sys.stderr)
        return True, []


def get_plan_sprint_tasks(plan_sprint_id: int) -> list[dict]:
    """Return backlog_items (subtasks) created during execution of this plan sprint."""
    try:
        with _connect() as con:
            row = con.execute(
                "SELECT run_id FROM plan_sprints WHERE id=?", (plan_sprint_id,)
            ).fetchone()
            if not row or not row["run_id"]:
                return []
            items = con.execute(
                """SELECT id, title, description, status, type, agent_type, target_files
                   FROM backlog_items WHERE run_id=? ORDER BY id ASC""",
                (row["run_id"],),
            ).fetchall()
            return [dict(r) for r in items]
    except Exception as e:
        print(f"[tracker] get_plan_sprint_tasks error: {e}", file=sys.stderr)
        return []


def get_memories(
    project_path: str | None = None,
    limit_project: int = 10,
    limit_global: int = 5,
) -> list[dict]:
    """
    Return memories ordered by relevance:
    - If project_path is given: up to limit_project project-specific memories (newest first),
      then up to limit_global memories from other projects (newest first).
    - If no project_path: return the most recent limit_global + limit_project memories globally.
    """
    try:
        with _connect() as con:
            if project_path:
                project_rows = con.execute(
                    """SELECT * FROM memories WHERE project_path=?
                       ORDER BY id DESC LIMIT ?""",
                    (project_path, limit_project),
                ).fetchall()
                global_rows = con.execute(
                    """SELECT * FROM memories WHERE project_path!=?
                       ORDER BY id DESC LIMIT ?""",
                    (project_path, limit_global),
                ).fetchall()
                return [dict(r) for r in project_rows] + [dict(r) for r in global_rows]
            else:
                rows = con.execute(
                    "SELECT * FROM memories ORDER BY id DESC LIMIT ?",
                    (limit_project + limit_global,),
                ).fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_memories error: {e}", file=sys.stderr)
        return []


# ── Subtask review gate ────────────────────────────────────────────────────────

def set_subtask_review_pending(run_id: int, subtask: dict, exec_result: dict) -> None:
    """Store a pending subtask review so the dashboard can display it."""
    try:
        summary = ""
        if isinstance(exec_result, dict):
            summary = exec_result.get("output") or exec_result.get("summary") or ""
            if isinstance(summary, str) and len(summary) > 500:
                summary = summary[:500] + "..."
        tests_passed = 1
        if isinstance(exec_result, dict):
            tests_passed = 0 if exec_result.get("tests_failed") else 1
        with _lock:
            with _connect() as con:
                # Clear any previous review for this run first
                con.execute("DELETE FROM subtask_reviews WHERE run_id=?", (run_id,))
                con.execute(
                    """INSERT INTO subtask_reviews
                       (run_id, subtask_id, description, agent_type, target_files, exec_summary, tests_passed, status)
                       VALUES (?,?,?,?,?,?,?,'pending')""",
                    (
                        run_id,
                        str(subtask.get("id", "")),
                        subtask.get("description", "")[:300],
                        subtask.get("agent_type", ""),
                        ",".join(subtask.get("target_files") or []),
                        summary,
                        tests_passed,
                    ),
                )
    except Exception as e:
        print(f"[tracker] set_subtask_review_pending error: {e}", file=sys.stderr)


def clear_subtask_review(run_id: int) -> None:
    """Remove the pending review record after it's been resolved."""
    try:
        with _lock:
            with _connect() as con:
                con.execute(
                    "UPDATE subtask_reviews SET status='resolved', resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE run_id=? AND status='pending'",
                    (run_id,),
                )
    except Exception as e:
        print(f"[tracker] clear_subtask_review error: {e}", file=sys.stderr)


def get_pending_subtask_reviews() -> list[dict]:
    """Return all currently pending subtask reviews."""
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT * FROM subtask_reviews WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_pending_subtask_reviews error: {e}", file=sys.stderr)
        return []


def set_subtask_clarification(run_id: int, subtask_id: str, question: str, options: list[str] | None = None) -> None:
    """Agent is stuck, needs human answer."""
    try:
        options_json = json.dumps(options) if options else None
        with _lock:
            with _connect() as con:
                # Clear any existing pending review for this subtask
                con.execute("DELETE FROM subtask_reviews WHERE run_id=? AND subtask_id=?", (run_id, subtask_id))
                con.execute(
                    """INSERT INTO subtask_reviews
                       (run_id, subtask_id, clarification_question, clarification_options, status)
                       VALUES (?, ?, ?, ?, 'awaiting_clarification')""",
                    (run_id, subtask_id, question, options_json),
                )
    except Exception as e:
        print(f"[tracker] set_subtask_clarification error: {e}", file=sys.stderr)


def get_pending_clarifications() -> list[dict]:
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT * FROM subtask_reviews WHERE status='awaiting_clarification' ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_pending_clarifications error: {e}", file=sys.stderr)
        return []


def resolve_subtask_clarification(run_id: int, subtask_id: str, answer: str) -> None:
    try:
        with _lock:
            with _connect() as con:
                con.execute(
                    """UPDATE subtask_reviews
                       SET clarification_response=?, status='resolved',
                           resolved_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
                       WHERE run_id=? AND subtask_id=? AND status='awaiting_clarification'""",
                    (answer, run_id, subtask_id),
                )
    except Exception as e:
        print(f"[tracker] resolve_subtask_clarification error: {e}", file=sys.stderr)


def add_partial_memory(
    run_id: int,
    subtask_id: str,
    outcome: str,
    lesson_hint: str | None = None,
) -> None:
    """Write an incremental subtask record for partial recovery and richer final memory."""
    try:
        with _lock:
            with _connect() as con:
                # Upsert: remove existing record for this subtask if any
                con.execute(
                    "DELETE FROM partial_memories WHERE run_id=? AND subtask_id=?",
                    (run_id, str(subtask_id)),
                )
                con.execute(
                    """INSERT INTO partial_memories
                       (run_id, subtask_id, outcome, lesson_hint)
                       VALUES (?, ?, ?, ?)""",
                    (run_id, str(subtask_id), outcome, lesson_hint),
                )
    except Exception as e:
        print(f"[tracker] add_partial_memory error: {e}", file=sys.stderr)


def get_partial_memories(run_id: int) -> list[dict]:
    """Return all incremental subtask records for a given run."""
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT * FROM partial_memories WHERE run_id=? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[tracker] get_partial_memories error: {e}", file=sys.stderr)
        return []


def get_from_subtask_cache(project_path: str, description: str, target_files: list[str]) -> str | None:
    """Check if a matching subtask patch exists in the cache."""
    import hashlib
    files_str = ",".join(sorted(target_files or []))
    subtask_hash = hashlib.sha256(f"{description}|{files_str}".encode()).hexdigest()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT patch FROM subtask_cache WHERE project_path=? AND subtask_hash=?",
                (project_path, subtask_hash)
            ).fetchone()
            return row["patch"] if row else None
    except Exception as e:
        print(f"[tracker] get_from_subtask_cache error: {e}", file=sys.stderr)
        return None


def add_to_subtask_cache(project_path: str, description: str, target_files: list[str], patch: str) -> None:
    """Save a successful subtask patch to the cache."""
    if not patch:
        return
    import hashlib
    files_str = ",".join(sorted(target_files or []))
    subtask_hash = hashlib.sha256(f"{description}|{files_str}".encode()).hexdigest()
    try:
        with _lock:
            with _connect() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO subtask_cache
                       (project_path, subtask_hash, description, target_files, patch)
                       VALUES (?, ?, ?, ?, ?)""",
                    (project_path, subtask_hash, description, files_str, patch)
                )
                conn.commit()
    except Exception as e:
        print(f"[tracker] add_to_subtask_cache error: {e}", file=sys.stderr)


async def clean_stale_runs():
    """
    Connects to the SQLite database (tracking.db) and deletes all records
    associated with runs that have a status of 'rejected', 'failed', or 'aborted'.
    The deletion also applies to related records in subtask_events,
    subtask_reviews, and memories tables before removing the entries
    from the runs table itself.
    """
    stale_statuses = ('rejected', 'failed', 'aborted')

    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT id FROM runs WHERE status IN (?, ?, ?)",
            stale_statuses
        )
        stale_run_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()

        if not stale_run_ids:
            return

        placeholders = ','.join(['?' for _ in stale_run_ids])

        # Clean up all related tables
        tables = [
            "subtask_events", "subtask_reviews", "memories", "backlog_items",
            "sprint_plans", "sprints", "plan_sprints", "partial_memories"
        ]
        for table in tables:
            await db.execute(
                f"DELETE FROM {table} WHERE run_id IN ({placeholders})",
                stale_run_ids
            )

        await db.execute(
            f"DELETE FROM runs WHERE id IN ({placeholders})",
            stale_run_ids
        )

        await db.commit()
