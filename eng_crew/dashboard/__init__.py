"""eng-crew dashboard — FastAPI web UI for monitoring runs and approving plans."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI
    from eng_crew.config import Settings


def create_app(settings: "Settings") -> "FastAPI":
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from eng_crew import tracker

    app = FastAPI(title="eng-crew dashboard", version="0.1.0")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/runs")
    def list_runs(limit: int = 50):
        try:
            import sqlite3
            db = settings.data_dir / "tracking.db"
            if not db.exists():
                return []
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/runs/{run_id}/approve")
    def approve_run(run_id: int):
        import json
        import sqlite3
        db = settings.data_dir / "tracking.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE runs SET hitl_decision=? WHERE id=?",
            (json.dumps({"approved": True}), run_id),
        )
        conn.commit()
        conn.close()
        return {"approved": True}

    @app.post("/api/runs/{run_id}/reject")
    def reject_run(run_id: int, feedback: str = ""):
        import json
        import sqlite3
        db = settings.data_dir / "tracking.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE runs SET hitl_decision=? WHERE id=?",
            (json.dumps({"approved": False, "feedback": feedback}), run_id),
        )
        conn.commit()
        conn.close()
        return {"approved": False}

    return app
