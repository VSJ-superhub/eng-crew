import os
import sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Mark this process as the dashboard so run_sprint uses dashboard HITL (in-process events).
os.environ["_DASHBOARD_PROCESS"] = "1"

from .routers import intake, projects, runs, system

app = FastAPI(title="eng-crew Dashboard", docs_url=None, redoc_url=None)

# Mount static assets if they exist (Vite build output)
_static_dir = os.path.join(os.path.dirname(__file__), "static")
_assets_dir = os.path.join(_static_dir, "assets")
if os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

_SPA_INDEX = Path(os.path.dirname(__file__)) / "static" / "index.html"

def _spa():
    if _SPA_INDEX.exists():
        return FileResponse(str(_SPA_INDEX))
    return HTMLResponse("<h1>Dashboard not built. Run: cd eng_crew/dashboard/frontend && npm run build</h1>", status_code=404)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(system.router)
app.include_router(intake.router)
app.include_router(projects.router)
app.include_router(runs.router)

# ── Pages (SPA catch-all) ─────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return _spa()

@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: int):
    return _spa()

@app.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request):
    return _spa()

@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    return _spa()

@app.get("/intake", response_class=HTMLResponse)
async def intake_page(request: Request):
    return _spa()

@app.get("/projects/{project_id}/tasks", response_class=HTMLResponse)
async def project_tasks_page(request: Request, project_id: int):
    return _spa()

# SPA catch-all for any other non-API routes
@app.get("/{full_path:path}", include_in_schema=False)
async def spa_catch_all(full_path: str):
    if full_path.startswith("api/"):
        return HTMLResponse("Not Found", status_code=404)
    return _spa()
