"""
SQLite-backed run + token tracking for the eng-crew pipeline.
Uses pathlib for cross-platform path handling.
The DB lives at ~/.eng-crew/tracking.db by default, or as configured in Settings.
"""
import json
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .config import settings

# ── Path configuration ───────────────────────────────────────────────────────
# Settings.data_dir defaults to .eng-crew (relative to project)
# But for public use, we might prefer Path.home() / ".eng-crew"
# The config.py we saw has data_dir: Path = Path(".eng-crew")

DATA_DIR = settings.data_dir
DB_PATH = DATA_DIR / "tracking.db"

# Ensure directory exists
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    print(f"[tracker] ERROR: Could not create data directory {DATA_DIR}: {e}", file=sys.stderr)
    # Fallback to home dir if project dir is not writable
    DATA_DIR = Path.home() / ".eng-crew"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DATA_DIR / "tracking.db"

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
                final_summary   TEXT,
                log_path        TEXT,
                hitl_decision   TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS subtask_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL REFERENCES runs(id),
                subtask_idx     INTEGER NOT NULL,
                agent_name      TEXT    NOT NULL,
                specialist_name TEXT    DEFAULT '',
                provider        TEXT    NOT NULL,
                model           TEXT    NOT NULL,
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                cost_usd        REAL    NOT NULL DEFAULT 0.0,
                result_text     TEXT,
                preprocessor_details TEXT,
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
                type            TEXT DEFAULT 'feature',
                project_id      INTEGER,
                run_id          INTEGER REFERENCES runs(id),
                subtask_id      TEXT,
                parent_issue_id INTEGER,
                active_sprint_id INTEGER,
                agent_type      TEXT DEFAULT '',
                target_files    TEXT DEFAULT '',
                diff_text       TEXT DEFAULT '',
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                sprint_id       INTEGER
            );

            CREATE TABLE IF NOT EXISTS sprint_plans (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER REFERENCES runs(id),
                plan_json       TEXT NOT NULL,
                design_text     TEXT DEFAULT '',
                critique_text   TEXT DEFAULT '',
                replans         TEXT DEFAULT '[]',
                business_summary TEXT DEFAULT '',
                approved        INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                approved_at     TEXT
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

_init_db()

# ... (CRUD functions here)
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def create_run(task_text: str, project_path: str, log_path: str | None = None) -> int:
    with _lock:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (task_text, project_path, started_at, log_path) VALUES (?, ?, ?, ?)",
                (task_text[:500], project_path, _now(), log_path),
            )
            return cur.lastrowid

def update_run_log_path(run_id: int, log_path: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute("UPDATE runs SET log_path=? WHERE id=?", (log_path, run_id))

def finish_run(run_id: int, status: str = "completed", final_summary: str = "") -> None:
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

def log_event(run_id: int, subtask_idx: int, agent_name: str, llm_result, specialist_name: str = "") -> None:
    try:
        result_text = (llm_result.text or "")[:8000]
        with _lock:
            with _connect() as conn:
                conn.execute(
                    """INSERT INTO subtask_events
                       (run_id, subtask_idx, agent_name, provider, model,
                        input_tokens, output_tokens, cost_usd, timestamp, specialist_name,
                        result_text)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (run_id, subtask_idx, agent_name, llm_result.provider, llm_result.model,
                     llm_result.input_tokens, llm_result.output_tokens, llm_result.cost_usd,
                     _now(), specialist_name, result_text),
                )
    except Exception as e:
        print(f"[tracker] log_event error: {e}", file=sys.stderr)

async def clean_stale_runs():
    stale_statuses = ('rejected', 'failed', 'aborted')
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute("SELECT id FROM runs WHERE status IN (?, ?, ?)", stale_statuses)
        stale_run_ids = [row[0] for row in await cursor.fetchall()]
        await cursor.close()
        if not stale_run_ids: return
        placeholders = ','.join(['?' for _ in stale_run_ids])
        tables = ["subtask_events", "subtask_reviews", "memories", "backlog_items", "sprint_plans"]
        for table in tables:
            await db.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", stale_run_ids)
        await db.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", stale_run_ids)
        await db.commit()

def get_run_usage(run_id: int) -> dict:
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
            return dict(row) if row else {}
    except Exception as e:
        print(f"[tracker] get_run_usage error: {e}", file=sys.stderr)
        return {}

def add_memory(run_id: int, project_path: str, date: str, title: str, task: str,
               what_worked: str, what_didnt: str, lesson: str, files_touched: str, raw_text: str) -> int:
    with _lock:
        with _connect() as con:
            cur = con.execute(
                """INSERT INTO memories
                   (run_id, project_path, date, title, task, what_worked,
                    what_didnt, lesson, files_touched, raw_text)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (run_id, project_path, date, title, task, what_worked, what_didnt, lesson, files_touched, raw_text),
            )
            return cur.lastrowid

def update_run_status(run_id: int, status: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))

def update_run_progress(run_id: int, idx: int, desc: str) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute(
                "UPDATE runs SET current_subtask_idx=?, current_subtask_desc=? WHERE id=?",
                (idx, desc, run_id),
            )

def get_hitl_decision(run_id: int):
    with _lock:
        with _connect() as conn:
            row = conn.execute("SELECT hitl_decision FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["hitl_decision"] is None:
                return None
            return json.loads(row["hitl_decision"])

def clear_hitl_decision(run_id: int) -> None:
    with _lock:
        with _connect() as conn:
            conn.execute("UPDATE runs SET hitl_decision=NULL WHERE id=?", (run_id,))
