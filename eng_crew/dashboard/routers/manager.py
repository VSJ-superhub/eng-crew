"""Dashboard API for the grounded Manager (PM) agent.

The Manager ideates against the real codebase and, on the user's accept, files a
ticket in the project's backlog — it does not start a build. This replaces the old
blind intake chat (claude -p --allowedTools none --max-turns 1) as the
conversational surface, and the old /dispatch endpoint, which fired a free-text
task straight at the builder with neither approval nor a definition of done.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/manager", tags=["manager"])


class ManagerChatPayload(BaseModel):
    message: str
    history: List[dict] = []
    project_path: str
    project_context: str = ""


class ManagerAcceptPayload(BaseModel):
    """An accepted ticket proposal. Criteria may arrive as a list or as lines."""
    title: str = ""
    description: str = ""
    acceptance_criteria: Union[List[str], str] = []
    rationale: str = ""
    project_path: str
    project_id: Optional[int] = None


class ManagerRememberPayload(BaseModel):
    project_path: str
    history: List[dict] = []
    project_name: str = ""


@router.post("/chat")
async def api_manager_chat(payload: ManagerChatPayload):
    """One grounded manager turn. Returns {reply, proposal} where proposal is
    {task, rationale} once a concrete buildable unit has emerged (else null)."""
    if not payload.project_path:
        return JSONResponse(
            {"error": "project_path is required — the manager grounds ideation in the real code."},
            status_code=400,
        )
    from ...manager import chat

    try:
        # chat() shells out to claude_cli with tools; keep the event loop free.
        result = await asyncio.to_thread(
            chat,
            payload.message,
            payload.history,
            payload.project_path,
            payload.project_context,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"reply": result.reply, "proposal": result.proposal})


@router.post("/accept")
async def api_manager_accept(payload: ManagerAcceptPayload):
    """File an accepted proposal as a backlog ticket. Starts no run.

    Returns {ticket, ticket_id, needs_acceptance_criteria, warning}. Approving the
    ticket to `ready` (PUT /api/tickets/{id}/status) is what releases it to build.
    """
    if not payload.title.strip() or not payload.project_path:
        return JSONResponse(
            {"error": "title and project_path are required."}, status_code=400
        )
    from ...manager import accept

    try:
        result = accept(
            {
                "title": payload.title,
                "description": payload.description,
                "acceptance_criteria": payload.acceptance_criteria,
                "rationale": payload.rationale,
            },
            payload.project_path,
            project_id=payload.project_id,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse(result)


@router.post("/remember")
async def api_manager_remember(payload: ManagerRememberPayload):
    """Distill an ideation session into the project's persistent vision memory.
    Best-effort; called on session end (e.g. the Ideate page reset)."""
    if not payload.project_path or not payload.history:
        return JSONResponse({"ok": True, "skipped": True})
    from ...manager import remember

    try:
        await asyncio.to_thread(
            remember, payload.project_path, payload.history, payload.project_name
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"ok": True})
