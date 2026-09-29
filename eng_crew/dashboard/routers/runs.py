from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Body
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Dict
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

from ...tracker import (
    get_claude_usage_stats,
    get_active_runs,
    get_recent_runs,
    get_run_events,
    get_run_detail,
    get_cost_by_model,
    get_sprint_plan,
    get_backlog_item,
    update_backlog_item,
    get_active_runs_for_project,
    get_run_subtask_statuses,
    list_issues,
    finish_run,
    set_pause_requested,
    get_project_by_path,
    get_pending_subtask_reviews,
    set_hitl_decision,
    get_runs_by_status,
)
from ...config import CLAUDE_WEEKLY_BUDGET
from ... import tickets

router = APIRouter(prefix="/api", tags=["runs"])

# ── Approval Models ───────────────────────────────────────────────────────────

class ApprovePayload(BaseModel):
    approved: bool
    feedback: str = ""
    selected_task_ids: Optional[List[str]] = None
    task_comments: Optional[Dict[str, str]] = None

class SubtaskReviewPayload(BaseModel):
    approved: bool

class ClarifyPayload(BaseModel):
    answer: str

# ── Runs API ──────────────────────────────────────────────────────────────────
@router.get("/run/{run_id}")
async def api_run_detail(run_id: int):
    run = get_run_detail(run_id)
    if not run: return JSONResponse({"error": "not found"}, status_code=404)

    raw_events = get_run_events(run_id) or []
    # ... (rest of processing)
    events, cost_by_agent = [], {}
    for e in raw_events:
        details = None
        if e.get("preprocessor_details"):
            try: details = json.loads(e["preprocessor_details"])
            except Exception: details = e["preprocessor_details"]
        events.append({
            "id": e.get("id"), "event_type": "info", "agent": e.get("agent_name") or e.get("specialist_name") or "",
            "message": e.get("result_text") or "", "created_at": e.get("timestamp") or "",
            "cost_usd": e.get("cost_usd"), "tokens_used": (e.get("input_tokens") or 0) + (e.get("output_tokens") or 0),
            "model": e.get("model") or "", "preprocessor_details": details,
            "subtask_idx": e.get("subtask_idx"),
        })
        ak = e.get("agent_name") or e.get("specialist_name") or "unknown"
        if ak not in cost_by_agent: cost_by_agent[ak] = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        cost_by_agent[ak]["cost_usd"] += e.get("cost_usd") or 0.0
        cost_by_agent[ak]["input_tokens"] += e.get("input_tokens") or 0
        cost_by_agent[ak]["output_tokens"] += e.get("output_tokens") or 0
    for ak, d in cost_by_agent.items():
        d["efficiency"] = d["output_tokens"] / d["input_tokens"] if d["input_tokens"] else "N/A"

    raw_plan = get_sprint_plan(run_id)
    plan = json.loads(raw_plan["plan_json"]) if raw_plan and raw_plan.get("plan_json") else []
    plan_summary = ""
    if raw_plan and raw_plan.get("plan_json"):
        try:
            full_plan = json.loads(raw_plan["plan_json"])
            if isinstance(full_plan, dict):
                plan_summary = full_plan.get("business_summary", "")
                plan = full_plan.get("subtasks", full_plan) if "subtasks" in full_plan else plan
            elif isinstance(full_plan, list):
                plan = full_plan
        except Exception:
            pass
    if plan:
        live = get_run_subtask_statuses(run_id)
        # Fetch diffs from backlog_items
        import sqlite3
        from ...tracker import DB_PATH
        diffs = {}
        try:
            with sqlite3.connect(DB_PATH) as conn:
                rows = conn.execute(
                    "SELECT subtask_id, diff_text FROM backlog_items WHERE run_id=?",
                    (run_id,)
                ).fetchall()
                diffs = {str(r[0]): r[1] for r in rows}
        except Exception:
            pass

        if live or diffs:
            for st in plan:
                sid = str(st.get("id", ""))
                if sid in live: 
                    st["status"] = live[sid]
                if sid in diffs:
                    st["diff"] = diffs[sid]
    # Fetch pending clarification if run status is 'awaiting_clarification'
    clarification = None
    if run.get("status") == "awaiting_clarification":
        try:
            from ...tracker import DB_PATH
            with sqlite3.connect(DB_PATH) as conn:
                row = conn.execute(
                    "SELECT subtask_id, clarification_question, clarification_options FROM subtask_reviews WHERE run_id=? AND status='awaiting_clarification' LIMIT 1",
                    (run_id,)
                ).fetchone()
                if row:
                    opts = []
                    if row[2]:
                        try: opts = json.loads(row[2])
                        except Exception: opts = []
                    clarification = {"subtask_id": row[0], "question": row[1], "options": opts}
        except Exception:
            pass

    return JSONResponse({
        "run": run, 
        "events": events, 
        "plan": plan, 
        "cost_by_agent": cost_by_agent, 
        "plan_summary": plan_summary,
        "clarification": clarification
    })

@router.post("/run/{run_id}/clarify")
async def api_run_clarify(run_id: int, payload: ClarifyPayload):
    from ...tracker import resolve_subtask_clarification, get_run_detail
    from ...run import resume_run
    run = get_run_detail(run_id)
    if not run: return JSONResponse({"error": "not found"}, status_code=404)
    
    # We need to know which subtask is awaiting clarification
    import sqlite3
    from ...tracker import DB_PATH
    subtask_id = None
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT subtask_id FROM subtask_reviews WHERE run_id=? AND status='awaiting_clarification' LIMIT 1",
            (run_id,)
        ).fetchone()
        if row: subtask_id = row[0]
    
    if not subtask_id:
        return JSONResponse({"error": "No pending clarification for this run"}, status_code=400)

    resolve_subtask_clarification(run_id, subtask_id, payload.answer)
    
    # Resume the run — it will load the state, see the answer in subtask, and continue
    if not resume_run(run_id, clarification_answer=payload.answer):
        return JSONResponse({"error": "State lost or could not resume"}, status_code=409)
        
    return JSONResponse({"ok": True})

# ── Launch & HITL ─────────────────────────────────────────────────────────────


def _python_exe() -> str:
    root = Path(__file__).parents[3]
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = root / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def launch_ticket_run(ticket_id: int, *, actor: str = "user",
                      require_approval: bool = False) -> tuple[bool, str]:
    """Claim a ticket and build it in a subprocess. Returns (started, detail).

    The ticket is claimed here rather than in the subprocess so an over-WIP or
    otherwise illegal request can be refused synchronously, with a reason. The
    subprocess then runs `eng-crew ticket <id>`, which builds the ticket's own
    text and links its run back to the ticket.
    """
    import subprocess
    import time

    ticket = get_backlog_item(ticket_id)
    if not ticket:
        return False, "not found"
    if get_active_runs_for_project(ticket["project_path"]):
        return False, "Already running for this project"

    try:
        tickets.move(ticket_id, tickets.IN_PROGRESS, actor, "build requested")
    except tickets.TransitionRefused as exc:
        return False, str(exc)

    root = Path(__file__).parents[3]
    log_file = root / "logs" / f"run_{int(time.time())}.log"
    log_file.parent.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "_DASHBOARD_PROCESS": "1"}
    if not require_approval:
        env["CI"] = "1"

    cmd = [_python_exe(), "-u", "-m", "eng_crew", "ticket", str(ticket_id),
           "--log-path", str(log_file)]
    if not require_approval:
        cmd.append("--no-approval")

    try:
        with open(log_file, "w") as log:
            subprocess.Popen(
                cmd, cwd=str(root), env=env, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
    except Exception as e:
        print(f"[dashboard] Failed to launch ticket {ticket_id}: {e}", file=sys.stderr)
        try:
            tickets.move(ticket_id, tickets.BACKLOG, actor, f"launch failed: {e}")
        except tickets.TransitionRefused:
            pass
        return False, str(e)
    return True, str(log_file)


def _launch_response(ticket_id: int, *, require_approval: bool = False):
    ticket = get_backlog_item(ticket_id)
    if not ticket:
        return JSONResponse({"error": "not found"}, status_code=404)
    if ticket["status"] == tickets.IN_PROGRESS:
        return JSONResponse({"error": "already running"}, status_code=400)

    started = threading.Event()
    outcome: dict = {}

    def _bg():
        outcome["ok"], outcome["detail"] = launch_ticket_run(
            ticket_id, require_approval=require_approval
        )
        started.set()

    threading.Thread(target=_bg, daemon=True).start()
    started.wait(timeout=30)
    if not outcome:
        return JSONResponse({"error": "launch timed out"}, status_code=504)
    if not outcome["ok"]:
        detail = outcome["detail"]
        code = 409 if ("Already running" in detail or "WIP limit" in detail) else 400
        return JSONResponse({"error": detail}, status_code=code)
    return JSONResponse({"ok": True, "log_path": outcome["detail"]})


@router.post("/backlog/{item_id}/run")
async def api_backlog_run(item_id: int):
    return _launch_response(item_id)


@router.post("/tickets/{ticket_id}/run")
async def api_ticket_run(ticket_id: int):
    return _launch_response(ticket_id)


@router.post("/tickets/{ticket_id}/run-with-review")
async def api_ticket_run_with_review(ticket_id: int):
    return _launch_response(ticket_id, require_approval=True)


@router.post("/runs/{run_id}/approve")
async def api_run_approve(run_id: int, payload: ApprovePayload):
    # HITL is bridged through the DB: the pipeline's hitl_gate_node polls
    # tracker.get_hitl_decision while it waits at "awaiting_approval".
    set_hitl_decision(run_id, {
        "approved": payload.approved,
        "feedback": payload.feedback or None,
        "selected_task_ids": payload.selected_task_ids,
        "task_comments": payload.task_comments,
    })
    return JSONResponse({"ok": True})

@router.get("/runs/awaiting-approval")
async def api_runs_awaiting():
    return JSONResponse([r["id"] for r in get_runs_by_status("awaiting_approval")])

@router.get("/runs/awaiting-subtask-review")
async def api_runs_awaiting_subtask_review():
    # Per-subtask human review is not wired into the current pipeline graph
    # (dispatcher -> reviewer -> executor -> orchestrator), so there is never
    # anything pending here. Kept as a stable endpoint for the frontend poller.
    return JSONResponse({"run_ids": [], "reviews": get_pending_subtask_reviews()})

@router.post("/runs/{run_id}/subtask-review")
async def api_subtask_review(run_id: int, payload: SubtaskReviewPayload):
    # No-op: subtask-review is not part of the active pipeline (see above).
    return JSONResponse({"ok": True})

@router.post("/runs/{run_id}/retry")
async def api_run_retry(run_id: int):
    from ...run import run_task as _run_task
    run = get_run_detail(run_id)
    if not run: return JSONResponse({"error": "not found"}, status_code=404)
    def _bg():
        try:
            # The pipeline's hitl_gate_node handles approval via the DB-poll
            # bridge automatically; no callbacks need to be passed in.
            _run_task(task=run["task_text"], project_path=run["project_path"],
                      claude_md_path=run.get("claude_md_path") or "")
        except Exception: pass
    threading.Thread(target=_bg, daemon=True).start()
    return JSONResponse({"ok": True})

@router.post("/runs/{run_id}/pause")
async def api_run_pause(run_id: int):
    set_pause_requested(run_id, True)
    return JSONResponse({"ok": True})

@router.post("/runs/{run_id}/resume")
async def api_run_resume(run_id: int):
    from ...run import resume_run
    from ...tracker import is_pause_requested
    # A live run waiting at a node only needs the flag cleared — restarting it
    # would run the task twice.
    if is_pause_requested(run_id):
        set_pause_requested(run_id, False)
        return JSONResponse({"ok": True, "unpaused": True})
    if not resume_run(run_id): return JSONResponse({"error": "State lost"}, status_code=409)
    return JSONResponse({"ok": True})

@router.post("/runs/{run_id}/cancel")
async def api_run_cancel(run_id: int):
    finish_run(run_id, status="failed", final_summary="Cancelled manually.")
    return JSONResponse({"ok": True})

# ── SSE ───────────────────────────────────────────────────────────────────────

@router.get("/status/stream")
async def api_status_stream():
    async def _gen():
        while True:
            try:
                payload = {"active_runs": get_active_runs(), "recent_runs": get_recent_runs(limit=15), "cost_by_model": get_cost_by_model()}
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e: yield f"data: {json.dumps({'error': str(e)})}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.get("/{run_id}/logs")
async def api_run_logs(run_id: int):
    from ...tracker import get_run_detail
    run = get_run_detail(run_id)
    if not run or not run.get("log_path"):
        raise HTTPException(status_code=404, detail="Log file not found for this run")

    log_path = Path(run["log_path"])
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"Log file {log_path} does not exist on disk")

    async def log_generator():
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            # First, send existing content
            content = f.read()
            yield content

            # Then, tail the file if the run is still active
            if run.get("status") in ("running", "awaiting_approval", "awaiting_clarification"):
                while True:
                    line = f.readline()
                    if line:
                        yield line
                    else:
                        # Check if run finished while we were waiting
                        from ...tracker import get_run_detail as _get_detail
                        updated_run = _get_detail(run_id)
                        if updated_run.get("status") not in ("running", "awaiting_approval", "awaiting_clarification"):
                            # Read one last time in case more was written
                            final_lines = f.read()
                            if final_lines:
                                yield final_lines
                            break
                        await asyncio.sleep(0.5)

    return StreamingResponse(log_generator(), media_type="text/plain")

@router.get("/{run_id}/events/stream")
async def api_run_events_stream(run_id: int):
    async def _gen():
        last_id, terminal = 0, {"completed", "failed", "aborted"}
        try:
            existing = get_run_events(run_id) or []
            for ev in existing:
                yield f"data: {json.dumps({'event': ev})}\n\n"
                if ev.get("id", 0) > last_id: last_id = ev["id"]
        except Exception: pass
        while True:
            await asyncio.sleep(1)
            try:
                run = get_run_detail(run_id)
                if not run: yield "data: {\"done\": true}\n\n"; return
                from ...tracker import _connect
                import sqlite3
                with _connect() as conn:
                    rows = conn.execute("SELECT * FROM subtask_events WHERE run_id=? AND id > ? ORDER BY id ASC", (run_id, last_id)).fetchall()
                for r in rows:
                    ev = dict(r)
                    yield f"data: {json.dumps({'event': ev, 'run_status': run['status']})}\n\n"
                    last_id = ev["id"]
                yield f"data: {json.dumps({'run': {'id': run['id'], 'status': run['status']}})}\n\n"
                if run["status"] in terminal: yield "data: {\"done\": true}\n\n"; return
            except Exception as e: yield f"data: {json.dumps({'error': str(e)})}\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.get("/runs/{run_id}/subtask/{subtask_idx}/files")
async def api_subtask_files(run_id: int, subtask_idx: int):
    run = get_run_detail(run_id)
    if not run: return JSONResponse({"error": "not found"}, status_code=404)
    raw_plan = get_sprint_plan(run_id)
    if not raw_plan or not raw_plan.get("plan_json"): return JSONResponse({"error": "plan not found"}, status_code=404)
    subtasks = json.loads(raw_plan["plan_json"])
    if subtask_idx < 0 or subtask_idx >= len(subtasks): return JSONResponse({"error": "range"}, status_code=404)
    subtask = subtasks[subtask_idx]
    project_path = run.get("project_path", "")
    files = []
    for rel in subtask.get("target_files", []):
        abs_p = Path(project_path) / rel if not Path(rel).is_absolute() else Path(rel)
        try: content = abs_p.read_text(encoding="utf-8", errors="replace")
        except: content = None
        files.append({"path": rel, "content": content})
    return JSONResponse({"files": files})

# ── Issues ───────────────────────────────────────────────────────────────────
#
# An "issue" is a ticket tagged type='issue'; it runs through the same queue and
# the same launcher as any other ticket.

class IssueIn(BaseModel):
    title: str
    description: str = ""
    priority: int = 50
    acceptance_criteria: str = ""

@router.get("/projects/{project_id}/issues")
async def api_project_issues(project_id: int, status: Optional[str] = None):
    from ...tracker import get_project
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"project": p, "issues": list_issues(project_id, status=status)})

@router.post("/projects/{project_id}/issues")
async def api_project_issues_create(project_id: int, item: IssueIn):
    from ...tracker import get_project, add_backlog_item
    p = get_project(project_id)
    if not p: return JSONResponse({"error": "not found"}, status_code=404)
    issue_id = add_backlog_item(title=item.title, description=item.description or item.title,
                                project_path=p["project_path"], claude_md_path=p["claude_md_path"],
                                priority=item.priority, project_id=project_id,
                                item_type="issue",
                                acceptance_criteria=item.acceptance_criteria)
    return JSONResponse({"id": issue_id})

@router.post("/issues/{issue_id}/run")
async def api_issue_run(issue_id: int):
    return _launch_response(issue_id)

@router.post("/issues/{issue_id}/run-with-review")
async def api_issue_run_with_review(issue_id: int):
    return _launch_response(issue_id, require_approval=True)
