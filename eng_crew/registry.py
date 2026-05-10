from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .config import Settings, settings as _default_settings


def list_projects(cfg: Settings | None = None) -> list[dict[str, Any]]:
    s = cfg or _default_settings
    db_path = s.data_dir / "tracking.db"

    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT
                project_path,
                COUNT(*) AS total_runs,
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS active_runs,
                MAX(started_at) AS last_run_at,
                COALESCE(SUM(total_cost_usd), 0.0) AS total_cost_usd
            FROM runs
            GROUP BY project_path
            ORDER BY last_run_at DESC
        """).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    projects = []
    for row in rows:
        path = row["project_path"]
        projects.append({
            "name": Path(path).name,
            "path": path,
            "total_runs": row["total_runs"],
            "active_runs": row["active_runs"],
            "last_run_at": row["last_run_at"],
            "total_cost_usd": round(row["total_cost_usd"], 4),
        })
    return projects
