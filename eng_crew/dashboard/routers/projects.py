from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
import os
import sys
import threading
import re
import json
from pathlib import Path

from ... import budget, tickets

from ...tracker import (
    add_backlog_item,
    list_backlog_items,
    update_backlog_item,
    delete_backlog_item,
    get_backlog_item,
    list_project_tickets,
    list_plan_tickets,
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
    get_active_runs_for_project,
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
    acceptance_criteria: str = ""
    # The roadmap column this ticket belongs to. Changing it later goes through
    # PUT /tickets/{id}/area, which records who did it.
    area: str = ""

class BacklogItemUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    acceptance_criteria: Optional[str] = None

class TicketStatusIn(BaseModel):
    """`status` alone is not enough: a status change is only worth recording if
    we know who made it."""
    status: str
    actor: str = "user"
    note: str = ""

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
    # Annotated with what each ticket waits on: the list offers a Build button,
    # and a blocked ticket's build would be refused.
    return JSONResponse([
        tickets.with_dependencies(t)
        for t in list_backlog_items(status=status, project_path=project)
    ])

@router.post("/backlog")
async def api_backlog_create(item: BacklogItemIn):
    desc = item.description or item.title
    project_id = item.project_id
    if project_id is None and item.project_path:
        p = get_project_by_path(item.project_path)
        if p: project_id = p["id"]
    item_id = add_backlog_item(item.title, desc, item.project_path, item.claude_md_path,
                               item.priority, project_id, item_type=item.item_type,
                               acceptance_criteria=item.acceptance_criteria,
                               area=item.area)
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

# ── Ticket lifecycle ──────────────────────────────────────────────────────────
#
# Status is deliberately not settable through the generic PUT above. A status
# change has to go through the lifecycle so the transition is legal, the
# acceptance-criteria gate and the WIP limit are checked, and an event is
# recorded naming who made it.

@router.get("/tickets/{ticket_id}")
async def api_ticket_get(ticket_id: int):
    ticket = get_backlog_item(ticket_id)
    if not ticket:
        return JSONResponse({"error": "not found"}, status_code=404)
    from ... import review

    # What this ticket waits on, and whether any of it is still unfinished: a
    # blocked ticket cannot be built, so the page has to say so rather than
    # offering a button that will be refused.
    annotated = tickets.with_dependencies(ticket)
    return JSONResponse({
        "ticket": annotated,
        "blocked": annotated["blocked"],
        "prerequisites": annotated["prerequisites"],
        "unmet_prerequisites": annotated["unmet_prerequisites"],
        # What this ticket may spend and what it has spent, side by side: the
        # number a person needs when a build was stopped for budget.
        "budget": budget.status(ticket_id),
        "events": tickets.events(ticket_id),
        # The verdict on the ticket's last review, pulled out of the history so
        # a ticket held back at in_review can show why without reading events.
        "review": review.latest_verdict(ticket_id),
        # In review with no verdict yet — stopped for budget, or the reviewer was
        # unreachable — so the page can offer to run it.
        "awaiting_review": review.awaiting_review(ticket_id) is not None,
        "allowed_transitions": sorted(tickets.allowed_transitions(ticket["status"])),
    })

@router.get("/tickets/{ticket_id}/events")
async def api_ticket_events(ticket_id: int):
    if not get_backlog_item(ticket_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"events": tickets.events(ticket_id)})

@router.put("/tickets/{ticket_id}/status")
async def api_ticket_status(ticket_id: int, payload: TicketStatusIn):
    if not get_backlog_item(ticket_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        ticket = tickets.move(ticket_id, payload.status, payload.actor, payload.note)
    except tickets.TransitionRefused as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "ticket": ticket})

class TicketBudgetIn(BaseModel):
    """A new spend authority for a ticket, and who authorised it."""
    budget_usd: float
    actor: str = "user"


@router.put("/tickets/{ticket_id}/budget")
async def api_ticket_budget(ticket_id: int, payload: TicketBudgetIn):
    """Change a ticket's budget. Raising it is what releases stopped work.

    A review the budget stopped starts again in the background once the budget
    allows it. No build starts: authorising more spend and deciding to build
    again are separate calls, and the second one is the person's.
    """
    if not get_backlog_item(ticket_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    if payload.budget_usd < 0:
        return JSONResponse({"error": "a budget cannot be negative"}, status_code=400)
    state = budget.set_budget(ticket_id, payload.budget_usd, payload.actor)
    started = bool(state and not state["exhausted"]) and _start_waiting_review(ticket_id)
    return JSONResponse({"ok": True, "budget": state, "review_started": started})


def _start_waiting_review(ticket_id: int) -> bool:
    """Run a ticket's waiting review off the request thread. False if none waits.

    The review is one model call, but a call is seconds to minutes; its verdict
    lands in the ticket's history, which the page re-reads.
    """
    from ... import review

    if review.awaiting_review(ticket_id) is None or review.reviewing(ticket_id):
        return False

    def work() -> None:
        try:
            review.review_if_waiting(ticket_id)
        except review.ReviewUnavailable as exc:
            print(f"[review] ticket {ticket_id} was not reviewed: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"[review] reviewing ticket {ticket_id} failed: {exc}", file=sys.stderr)

    threading.Thread(target=work, daemon=True, name=f"review-{ticket_id}").start()
    return True


@router.post("/tickets/{ticket_id}/review")
async def api_ticket_review(ticket_id: int):
    """Run the review a ticket in review is still waiting on.

    Refused for a ticket that already has a verdict: acting on a verdict is a
    person's call, not a reason to ask the reviewer again.
    """
    if not get_backlog_item(ticket_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    state = budget.status(ticket_id)
    if state and state["exhausted"]:
        return JSONResponse(
            {"error": "this ticket has spent its budget — raise it first"}, status_code=409,
        )
    from ... import review

    if review.reviewing(ticket_id):
        return JSONResponse(
            {"error": "a review of this ticket is already running — its verdict "
                      "will appear in the history"}, status_code=409,
        )
    if not _start_waiting_review(ticket_id):
        return JSONResponse(
            {"error": "this ticket is not waiting on a review"}, status_code=409,
        )
    return JSONResponse({"ok": True, "started": True})


class TicketAreaIn(BaseModel):
    """Which part of the product a ticket belongs to, and who decided."""
    area: str = ""
    actor: str = "user"


@router.put("/tickets/{ticket_id}/area")
async def api_ticket_area(ticket_id: int, payload: TicketAreaIn):
    """Put a ticket in an area, or (with an empty one) take it out of one.

    Recorded in the ticket's history with the actor, like a status change: the
    roadmap is read as a statement about the product, so who moved a ticket
    from Engine to Website is worth keeping.
    """
    if not get_backlog_item(ticket_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        area = tickets.set_area(ticket_id, payload.area, payload.actor)
    except tickets.TransitionRefused as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "area": area})


@router.get("/projects/{project_id}/roadmap")
async def api_project_roadmap(project_id: int):
    """The project's tickets grouped by area, in dependency order.

    The queue endpoint above answers "what is in flight?"; this answers "what
    is the shape of this product, and what can be started next?". The states
    are computed in :mod:`eng_crew.tickets` rather than in the frontend, so the
    Roadmap tab and Discord's `/roadmap` cannot disagree.
    """
    p = get_project(project_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    board = tickets.roadmap(p["project_path"])
    return JSONResponse({"project": p, **board})


@router.get("/projects/{project_id}/queue")
async def api_project_queue(project_id: int):
    """The continuous queue: every ticket, plus WIP accounting."""
    p = get_project(project_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    from ... import review

    all_tickets = [
        {
            **tickets.with_dependencies(budget.annotate(t)),
            # In review with no verdict yet, so the card can offer to run it.
            "awaiting_review": t["status"] == tickets.IN_REVIEW
            and review.awaiting_review(t["id"]) is not None,
        }
        for t in list_project_tickets(p["project_path"])
    ]
    return JSONResponse({
        "project": p,
        "tickets": all_tickets,
        # `ready` is what the queue would actually pull from, so a ready ticket
        # whose prerequisites are unfinished is not in it.
        "ready": [t for t in all_tickets
                  if t["status"] == tickets.READY and not t["blocked"]],
        "blocked": [t for t in all_tickets
                    if t["status"] == tickets.READY and t["blocked"]],
        "counts": tickets.summarize(all_tickets),
        "wip": tickets.wip(p["project_path"]),
    })

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

# ── Filesystem access guard ───────────────────────────────────────────────────
#
# The dashboard has no authentication and these endpoints take a path from the
# request. Unconstrained, they let anything that can reach the port read any
# file on the machine (~/.ssh/id_rsa, a .env full of API keys) and drop a
# CLAUDE.md into any directory. Confining them to registered project roots
# keeps the feature and removes the hole.

def _registered_roots() -> list[Path]:
    roots: list[Path] = []
    for proj in list_projects(active_only=False):
        raw = proj.get("project_path")
        if not raw:
            continue
        try:
            roots.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return roots


def _within_registered_project(path: str) -> Optional[Path]:
    """Resolve path and return it only if it sits inside a registered project.

    Both sides are fully resolved first, so `..` traversal and symlinks out of a
    project resolve to their real location and are rejected on their merits.
    """
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, ValueError):
        return None

    target_cmp = os.path.normcase(str(target))
    for root in _registered_roots():
        root_cmp = os.path.normcase(str(root))
        if target_cmp == root_cmp or target_cmp.startswith(root_cmp + os.sep):
            return target
    return None


_OUTSIDE = {"error": "path is outside every registered project"}


@router.get("/fs/read-file")
async def fs_read_file(path: str):
    target = _within_registered_project(path)
    if target is None:
        return JSONResponse(_OUTSIDE, status_code=403)
    try:
        return JSONResponse({"content": target.read_text(encoding="utf-8", errors="replace")})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

class WriteClaudeMdPayload(BaseModel):
    path: str
    content: str

@router.post("/fs/write-claude-md")
async def fs_write_claude_md(payload: WriteClaudeMdPayload):
    directory = _within_registered_project(payload.path)
    if directory is None:
        return JSONResponse(_OUTSIDE, status_code=403)
    claude_md = directory / "CLAUDE.md"
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

def _start_planning(project_id: int, project: dict, goal: str, force: bool):
    """Plan a project in the background: produce tickets, not a build.

    Returns (payload, status_code). The guard is against a second planner
    running while the first is still thinking — planning costs money, and a
    double-click should not buy it twice.
    """
    existing = get_active_plan(project_id)
    if existing and not force:
        planned = list_plan_tickets(existing["id"])
        if not planned and not (existing.get("review_result") or ""):
            return {"error": "Planning in progress"}, 409

    plan_id = create_project_plan(project_id, goal)

    def _bg():
        from ...run import run_project as _run_project
        try:
            _run_project(goal=goal, project_id=project_id,
                         project_path=project["project_path"],
                         claude_md_path=project.get("claude_md_path") or "",
                         plan_id=plan_id)
        except Exception as e:
            print(f"[dashboard] planning failed for project {project_id}: {e}", file=sys.stderr)

    threading.Thread(target=_bg, daemon=True).start()
    return {"plan_id": plan_id, "tickets": []}, 200


@router.post("/projects/{project_id}/plan-from-claude-md")
async def api_plan_from_claude_md(project_id: int):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    path = p.get("claude_md_path") or str(Path(p["project_path"]) / "CLAUDE.md")
    brief = _extract_planning_brief(path)
    goal = "Plan based on CLAUDE.md outstanding work:\n\n" + brief
    body, code = _start_planning(project_id, {**p, "claude_md_path": path}, goal, force=False)
    if code == 200:
        body["source"] = "claude_md"
    return JSONResponse(body, status_code=code)

@router.post("/projects/{project_id}/plan")
async def api_create_project_plan(project_id: int, payload: PlanGoalPayload):
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    body, code = _start_planning(project_id, p, payload.goal, force=payload.force)
    return JSONResponse(body, status_code=code)

@router.get("/projects/{project_id}/plan")
async def api_get_project_plan(project_id: int):
    """The active plan and the tickets it produced.

    `planning` is true while the planner is still working; `planning_failed`
    when it finished without producing any.
    """
    from datetime import datetime, timezone, timedelta
    plan = get_active_plan(project_id)
    if not plan:
        return JSONResponse({"plan": None, "tickets": [], "planning": False})
    planned = list_plan_tickets(plan["id"])
    if planned:
        return JSONResponse({"plan": plan, "tickets": planned, "planning": False})

    error = plan.get("review_result") or ""
    failed = error.startswith("planning failed")
    if not failed:
        try:
            created = datetime.fromisoformat(plan["created_at"].replace("Z", "+00:00"))
            failed = datetime.now(timezone.utc) - created > timedelta(minutes=5)
        except Exception:
            pass
    return JSONResponse({
        "plan": plan, "tickets": [], "planning": not failed,
        "planning_failed": failed, "error": error,
    })

@router.post("/projects/{project_id}/queue/run")
async def api_run_queue(project_id: int):
    """Pull the next ticket off the queue and build it.

    This is what "run queued work" means now: the highest-priority `ready`
    ticket, built from its own text. Two limits apply and they are different
    things — the WIP limit bounds how many tickets are in flight at all
    (including the ones sitting in review and QA), while a project builds one
    ticket at a time, because runs share the project's checkout. So this starts
    at most one build per call and reports what is still waiting.
    """
    from .runs import launch_ticket_run

    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    path = p["project_path"]

    state = tickets.wip(path)
    # Everything ready, blocked ones included: they are still waiting, they
    # just cannot be pulled yet, and the caller is told which and why.
    waiting = tickets.queue(path, include_blocked=True)
    if state["slots"] <= 0:
        return JSONResponse(
            {"error": f"WIP limit reached ({state['in_flight']}/{state['limit']} in flight)",
             "started": [], "refused": [], "waiting": len(waiting), "wip": state},
            status_code=409,
        )
    # The interlock is the in_progress ticket itself, not the run row: a run row
    # only appears once the launched build gets going, so checking that would
    # let two rapid calls both pull from the queue.
    building = list_project_tickets(path, status=tickets.IN_PROGRESS)
    if building or get_active_runs_for_project(path):
        return JSONResponse(
            {"error": "Already running for this project",
             "started": [], "refused": [], "waiting": len(waiting), "wip": state},
            status_code=409,
        )
    if not waiting:
        return JSONResponse({"started": [], "refused": [], "waiting": 0, "wip": state})

    # A ticket that has spent its budget is waiting on a person, not on the
    # queue. Pulling it would be refused every time and block everything behind
    # it, so the queue passes over it and says so.
    #
    # A blocked ticket is passed over for the same reason: its prerequisites
    # are unfinished, so the build would be refused and everything behind it
    # would wait on work nobody is doing.
    held = []
    pullable = []
    for t in waiting:
        unmet = tickets.unmet_prerequisites(t)
        if unmet:
            held.append({
                "ticket_id": t["id"],
                "detail": f"blocked by {tickets.describe_prerequisites(unmet)}",
            })
        elif (budget.status(t["id"]) or {}).get("exhausted"):
            held.append({"ticket_id": t["id"],
                         "detail": "over budget — needs a human decision"})
        else:
            pullable.append(t)
    if not pullable:
        return JSONResponse({"started": [], "refused": held,
                             "waiting": len(waiting), "wip": state})

    ticket = pullable[0]
    ok, detail = launch_ticket_run(ticket["id"], actor="queue")
    entry = {"ticket_id": ticket["id"], "detail": detail}
    return JSONResponse({
        "started": [entry] if ok else [],
        "refused": held + ([] if ok else [entry]),
        "waiting": len(waiting) - (1 if ok else 0),
        "wip": tickets.wip(path),
    })
