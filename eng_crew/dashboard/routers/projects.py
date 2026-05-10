from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
import os
import sys
import threading
import re
import json
from pathlib import Path

from ...tracker import (
    add_backlog_item,
    list_backlog_items,
    update_backlog_item,
    delete_backlog_item,
    get_backlog_item,
    add_project,
    list_projects,
    get_project,
    get_project_by_path,
    get_project_task_summary,
    get_project_runs,
    get_project_stats,
    update_project,
    delete_project,
    clear_project_tasks,
    clear_all_tasks,
    create_project_plan,
    get_active_plan,
    get_plan_sprints,
    update_plan_sprint,
    get_plan_sprint_tasks,
    get_active_runs_for_project,
    create_project_plan,
)

router = APIRouter(prefix="/api", tags=["projects"])

# ── Backlog Models ────────────────────────────────────────────────────────────

class BacklogItemIn(BaseModel):
    title: str
    description: str = ""
    project_path: str = ""
    claude_md_path: str = ""
    priority: int = 50
    project_id: Optional[int] = None
    item_type: str = "feature"

class BacklogItemUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    status: Optional[str] = None

# ── Project Models ────────────────────────────────────────────────────────────

class ProjectIn(BaseModel):
    name: str
    project_path: str
    claude_md_path: str = ""
    repo_url: str = ""
    default_branch: str = "main"
    tech_stack: List[str] = []
    test_command: str = "pytest"
    description: str = ""

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    repo_url: Optional[str] = None
    default_branch: Optional[str] = None
    tech_stack: Optional[List[str]] = None
    test_command: Optional[str] = None
    description: Optional[str] = None
    active: Optional[int] = None

class PlanGoalPayload(BaseModel):
    goal: str
    force: bool = False

# ── Backlog API ───────────────────────────────────────────────────────────────

@router.get("/backlog")
async def api_backlog_list(status: Optional[str] = None, project: Optional[str] = None):
    return JSONResponse(list_backlog_items(status=status, project_path=project))

@router.post("/backlog")
async def api_backlog_create(item: BacklogItemIn):
    desc = item.description or item.title
    project_id = item.project_id
    if project_id is None and item.project_path:
        p = get_project_by_path(item.project_path)
        if p: project_id = p["id"]
    item_id = add_backlog_item(item.title, desc, item.project_path, item.claude_md_path, item.priority, project_id, item_type=item.item_type)
    return JSONResponse({"id": item_id})

@router.put("/backlog/{item_id}")
async def api_backlog_update(item_id: int, item: BacklogItemUpdate):
    fields = {k: v for k, v in item.model_dump().items() if v is not None}
    update_backlog_item(item_id, **fields)
    return JSONResponse({"ok": True})

@router.delete("/backlog/{item_id}")
async def api_backlog_delete(item_id: int):
    delete_backlog_item(item_id)
    return JSONResponse({"ok": True})

# ── Projects API ──────────────────────────────────────────────────────────────

@router.get("/projects")
async def api_projects_list():
    projects = list_projects(active_only=False)
    for p in projects:
        p["stats"] = get_project_stats(p["project_path"])
    return JSONResponse(projects)

@router.post("/projects")
async def api_projects_create(item: ProjectIn):
    from ...project_context import resolve_context_md_path
    if not os.path.isdir(item.project_path):
        return JSONResponse({"error": f"Project folder not found: {item.project_path}"}, status_code=400)
    resolved_md = resolve_context_md_path(item.project_path, item.claude_md_path or None)
    project_id = add_project(name=item.name, project_path=item.project_path, claude_md_path=resolved_md,
                             repo_url=item.repo_url, default_branch=item.default_branch, tech_stack=item.tech_stack,
                             test_command=item.test_command, description=item.description)
    def _entroly_init_bg():
        try:
            from ...entroly_integration import init_project
            init_project(item.project_path)
        except Exception: pass
    threading.Thread(target=_entroly_init_bg, daemon=True).start()
    return JSONResponse({"id": project_id})

@router.get("/projects/task-summary")
async def api_project_task_summary():
    return JSONResponse(get_project_task_summary())

@router.get("/projects/{project_id}")
async def api_projects_get(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(p)

@router.put("/projects/{project_id}")
async def api_projects_update(project_id: int, item: ProjectUpdate):
    fields = {k: v for k, v in item.model_dump().items() if v is not None}
    update_project(project_id, **fields)
    return JSONResponse({"ok": True})

@router.delete("/projects/{project_id}")
async def api_projects_delete(project_id: int):
    delete_project(project_id)
    return JSONResponse({"ok": True})

@router.get("/projects/{project_id}/runs")
async def api_project_runs(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"runs": get_project_runs(p["project_path"]), "stats": get_project_stats(p["project_path"])})

@router.get("/projects/{project_id}/tasks")
async def api_project_tasks(project_id: int, status: Optional[str] = None, type: Optional[str] = None):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"project": p, "tasks": list_backlog_items(status=status, project_path=p["project_path"], type=type)})

@router.get("/projects/{project_id}/features")
async def api_project_features(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    
    # In this system, 'features' are backlog_items of type 'feature' or 'issue'
    features_raw = list_backlog_items(project_path=p["project_path"])
    features = [f for f in features_raw if f.get("type") in ("feature", "issue")]
    
    # For each feature, find associated sprints (currently via project_plans)
    # Note: The mapping between backlog_items (features) and project_plans/sprints
    # is currently indirect. We'll simplify: 
    # If a plan exists, its sprints are the "active" work.
    plan = get_active_plan(project_id)
    sprints = get_plan_sprints(plan["id"]) if plan else []
    
    result = []
    for f in features:
        # Calculate progress based on sprints if linked, or just status
        # For now, let's treat the project plan sprints as the source of progress if they exist
        f_sprints = [s for s in sprints if s.get("name") == f["title"] or f["title"] in s.get("description", "")]
        
        done = len([s for s in f_sprints if s["status"] == "done"])
        total = len(f_sprints)
        progress = (done / total * 100) if total > 0 else (100 if f["status"] == "done" else 0)
        
        result.append({
            "id": f["id"],
            "title": f["title"],
            "description": f["description"],
            "status": f["status"],
            "project_id": project_id,
            "progress": progress,
            "sprint_count": total,
            "done_sprints": done
        })
    
    # If no features in backlog but a plan exists, treat the plan goal as a feature
    if not result and plan:
        done = len([s for s in sprints if s["status"] == "done"])
        total = len(sprints)
        goal = plan["goal"]
        
        # If the goal is very long, use a generic title and put the goal in description
        if len(goal) > 100:
            title = "Project Roadmap"
            description = goal
        else:
            title = goal
            description = "Main Project Goal"

        result.append({
            "id": plan["id"],
            "title": title,
            "description": description,
            "status": "active",
            "project_id": project_id,
            "progress": (done / total * 100) if total > 0 else 0,
            "sprint_count": total,
            "done_sprints": done
        })

    return JSONResponse({"features": result})

@router.delete("/projects/{project_id}/tasks")
async def api_clear_project_tasks(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    count = clear_project_tasks(project_id)
    return JSONResponse({"ok": True, "deleted": count})

@router.delete("/tasks/all")
async def api_clear_all_tasks():
    counts = clear_all_tasks()
    return JSONResponse({"ok": True, "counts": counts})

# ── Filesystem ────────────────────────────────────────────────────────────────

_PROJECTS_ROOT = str(Path.home() / "Projects")
_STEERING_NAMES = {"CLAUDE.md", "AGENTS.md", "SOUL.md", "ARCHITECTURE.md", "MEMORY.md"}

@router.get("/fs/browse")
async def fs_browse(path: str = "", files: bool = False):
    base = Path(path) if path else Path(_PROJECTS_ROOT)
    if not base.exists() or not base.is_dir(): base = Path(_PROJECTS_ROOT)
    try:
        entries = list(base.iterdir())
        dirs = sorted([d for d in entries if d.is_dir() and not d.name.startswith(".")], key=lambda d: d.name.lower())
        result = {
            "current": str(base).replace("\\", "/"),
            "parent": str(base.parent).replace("\\", "/") if base.parent != base else None,
            "dirs": [{"name": d.name, "path": str(d).replace("\\", "/")} for d in dirs],
        }
        if files:
            md_files = sorted([f for f in entries if f.is_file() and f.suffix.lower() == ".md"],
                              key=lambda f: (0 if f.name in _STEERING_NAMES else 1, f.name.lower()))
            result["files"] = [{"name": f.name, "path": str(f).replace("\\", "/"), "steering": f.name in _STEERING_NAMES} for f in md_files]
        return JSONResponse(result)
    except PermissionError: return JSONResponse({"error": "Permission denied"}, status_code=403)

def _detect_project(path: str) -> dict:
    root = Path(path)
    tech_stack, name, test_command = [], root.name, "pytest"
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            if data.get("name"): name = data["name"].replace("-", " ").replace("_", " ").title()
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            if "next" in deps: tech_stack.append("Next.js")
            elif "react" in deps: tech_stack.append("React")
            if "typescript" in deps or (root / "tsconfig.json").exists(): tech_stack.append("TypeScript")
            if "tailwindcss" in deps: tech_stack.append("Tailwind CSS")
            test_command = "npm test"
        except Exception: pass
    pyp = root / "pyproject.toml"
    if pyp.exists():
        content = pyp.read_text(encoding="utf-8", errors="replace").lower()
        m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', content)
        if m: name = m.group(1).replace("-", " ").replace("_", " ").title()
        for kw, lbl in [("fastapi", "FastAPI"), ("django", "Django"), ("langgraph", "LangGraph")]:
            if kw in content: tech_stack.append(lbl)
    claude_md = root / "CLAUDE.md"
    return {"name": name, "tech_stack": list(dict.fromkeys(tech_stack)), "claude_md_path": str(claude_md).replace("\\", "/"),
            "has_claude_md": claude_md.exists(), "test_command": test_command, "is_git_repo": (root / ".git").exists(), "default_branch": "main"}

class ScanPayload(BaseModel):
    path: str

@router.post("/fs/scan")
async def fs_scan(payload: ScanPayload):
    if not os.path.isdir(payload.path): return JSONResponse({"error": "Not a directory"}, status_code=400)
    return JSONResponse(_detect_project(payload.path))

@router.get("/fs/read-file")
async def fs_read_file(path: str):
    try: return JSONResponse({"content": Path(path).read_text(encoding="utf-8", errors="replace")})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

class WriteClaudeMdPayload(BaseModel):
    path: str
    content: str

@router.post("/fs/write-claude-md")
async def fs_write_claude_md(payload: WriteClaudeMdPayload):
    claude_md = Path(payload.path) / "CLAUDE.md"
    try:
        claude_md.write_text(payload.content, encoding="utf-8")
        return JSONResponse({"ok": True, "path": str(claude_md).replace("\\", "/")})
    except Exception as e: return JSONResponse({"error": str(e)}, status_code=500)

# ── Architecture ──────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/architecture")
async def api_project_architecture(project_id: int):
    from ...project_context import resolve_context_md_path
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    path = resolve_context_md_path(p["project_path"], p.get("claude_md_path"))
    if not os.path.isfile(path):
        return JSONResponse({"error": f"File not found: {path}", "content": None, "path": path})
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
        return JSONResponse({"content": content, "path": path, "filename": os.path.basename(path)})
    except Exception as e:
        return JSONResponse({"error": str(e), "content": None, "path": path})

@router.post("/projects/{project_id}/update-architecture")
async def api_update_architecture(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    project_path = p["project_path"]
    arch_path = Path(project_path) / "ARCHITECTURE.md"
    current = arch_path.read_text(encoding="utf-8", errors="replace") if arch_path.exists() else ""
    git_context = ""
    try:
        import subprocess as _sp
        diff = _sp.run(["git", "diff", "HEAD~5", "HEAD", "--stat"], cwd=project_path, capture_output=True, text=True, timeout=10)
        log = _sp.run(["git", "log", "--oneline", "-10"], cwd=project_path, capture_output=True, text=True, timeout=10)
        git_context = f"=== Recent commits ===\n{log.stdout}\n\n=== Changed files (last 5 commits) ===\n{diff.stdout}"
    except Exception: pass

    prompt = (
        "You are a senior staff engineer. Update the ARCHITECTURE.md below to reflect recent changes.\n\n"
        f"{git_context}\n\n=== CURRENT ARCHITECTURE.md ===\n{current or 'Write from scratch.'}"
    )
    from .intake import _run_claude_streaming
    async def _stream_and_save():
        chunks = []
        async for chunk in _run_claude_streaming(prompt):
            yield chunk
            try:
                data = json.loads(chunk[len("data: "):].strip())
                if "text" in data: chunks.append(data["text"])
            except Exception: pass
        if chunks:
            try: arch_path.write_text("".join(chunks), encoding="utf-8")
            except Exception: pass
    return StreamingResponse(_stream_and_save(), media_type="text/event-stream")

# ── Planning ──────────────────────────────────────────────────────────────────

_PLANNING_SECTIONS = ["## What Needs to Be Built", "## Phase Priorities", "## What Already Exists", "## Technical Constraints"]

def _extract_planning_brief(path: str) -> str:
    try: content = Path(path).read_text(encoding="utf-8")
    except Exception: return ""
    lines, sections, current = content.splitlines(), {}, None
    for line in lines:
        matched = next((s for s in _PLANNING_SECTIONS if line.strip().startswith(s)), None)
        if matched:
            current = matched
            sections[current] = []
        elif current:
            if line.startswith("## ") and not any(line.strip().startswith(s) for s in _PLANNING_SECTIONS):
                current = None
            else: sections[current].append(line)
    if not sections: return content
    return "\n\n".join(f"{s}\n" + "\n".join(sections[s]).strip() for s in _PLANNING_SECTIONS if s in sections)

@router.post("/projects/{project_id}/plan-from-claude-md")
async def api_plan_from_claude_md(project_id: int):
    from ...run import run_project as _run_project
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    existing = get_active_plan(project_id)
    if existing and not get_plan_sprints(existing["id"]):
        return JSONResponse({"error": "Planning in progress"}, status_code=409)
    path = p.get("claude_md_path") or str(Path(p["project_path"]) / "CLAUDE.md")
    brief = _extract_planning_brief(path)
    goal = f"Plan based on CLAUDE.md outstanding work:\n\n{brief}"
    plan_id = create_project_plan(project_id, goal)
    def _bg():
        try: _run_project(goal=goal, project_id=project_id, project_path=p["project_path"], claude_md_path=path, plan_id=plan_id)
        except Exception: pass
    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"plan_id": plan_id, "sprints": [], "source": "claude_md"})

@router.post("/projects/{project_id}/plan")
async def api_create_project_plan(project_id: int, payload: PlanGoalPayload):
    from ...run import run_project as _run_project
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    existing = get_active_plan(project_id)
    if existing and not get_plan_sprints(existing["id"]) and not payload.force:
        return JSONResponse({"error": "Planning in progress"}, status_code=409)
    plan_id = create_project_plan(project_id, payload.goal)
    def _bg():
        try: _run_project(goal=payload.goal, project_id=project_id, project_path=p["project_path"], claude_md_path=p["claude_md_path"], plan_id=plan_id)
        except Exception: pass
    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"plan_id": plan_id, "sprints": []})

@router.get("/projects/{project_id}/plan")
async def api_get_project_plan(project_id: int):
    from datetime import datetime, timezone, timedelta
    plan = get_active_plan(project_id)
    if not plan: return JSONResponse({"plan": None, "sprints": [], "planning": False})
    sprints = get_plan_sprints(plan["id"])
    if len(sprints) == 0:
        planning_failed = False
        try:
            created = datetime.fromisoformat(plan["created_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - created > timedelta(minutes=5):
                planning_failed = True
        except Exception:
            pass
        if planning_failed:
            return JSONResponse({"plan": plan, "sprints": [], "planning": False, "planning_failed": True})
    return JSONResponse({"plan": plan, "sprints": sprints, "planning": len(sprints) == 0})

@router.put("/plan-sprints/{plan_sprint_id}")
async def api_update_plan_sprint(plan_sprint_id: int, payload: dict):
    update_plan_sprint(plan_sprint_id, **{k: v for k, v in payload.items() if v is not None})
    return JSONResponse({"ok": True})

@router.get("/plan-sprints/{plan_sprint_id}/tasks")
async def api_plan_sprint_tasks(plan_sprint_id: int):
    return JSONResponse({"tasks": get_plan_sprint_tasks(plan_sprint_id)})

@router.post("/plan-sprints/{plan_sprint_id}/run")
async def api_run_plan_sprint(plan_sprint_id: int):
    from ...run import run_sprint as _run_sprint
    from ...tracker import _connect
    with _connect() as conn:
        row = conn.execute("SELECT plan_id FROM plan_sprints WHERE id=?", (plan_sprint_id,)).fetchone()
    if not row: return JSONResponse({"error": "not found"}, status_code=404)
    def _bg():
        try: _run_sprint(plan_sprint_id)
        except Exception: pass
    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"ok": True})
