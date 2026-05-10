from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import Settings, settings as _default_settings


def get_sprint_plans(
    project_path: str | Path,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    s = settings or _default_settings
    db_path = s.data_dir / "tracking.db"

    if not db_path.exists():
        return []

    project_path_str = str(Path(project_path).resolve())

    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                sp.id,
                sp.run_id,
                sp.plan_json,
                sp.design_text,
                sp.critique_text,
                sp.business_summary,
                sp.approved,
                sp.created_at,
                sp.approved_at,
                r.task_text,
                r.status AS run_status
            FROM sprint_plans sp
            JOIN runs r ON sp.run_id = r.id
            WHERE r.project_path = ?
            ORDER BY sp.created_at DESC
            """,
            (project_path_str,),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    plans: list[dict[str, Any]] = []
    for row in rows:
        try:
            plan_data = json.loads(row["plan_json"]) if row["plan_json"] else {}
        except (json.JSONDecodeError, TypeError):
            plan_data = {}

        title = (
            plan_data.get("title")
            or (row["task_text"][:80] if row["task_text"] else "Untitled sprint")
        )
        status = "approved" if row["approved"] else "pending"

        plans.append(
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "title": title,
                "status": status,
                "approved": bool(row["approved"]),
                "business_summary": row["business_summary"] or "",
                "design_text": row["design_text"] or "",
                "critique_text": row["critique_text"] or "",
                "plan": plan_data,
                "task_text": row["task_text"] or "",
                "run_status": row["run_status"] or "",
                "created_at": row["created_at"] or "",
                "approved_at": row["approved_at"] or "",
            }
        )

    return plans
