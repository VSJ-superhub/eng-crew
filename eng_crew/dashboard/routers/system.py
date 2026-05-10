from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...tracker import get_claude_usage_stats
from ...config import CLAUDE_WEEKLY_BUDGET

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/providers")
async def api_providers():
    from ...providers import has_credentials
    providers = ["claude_cli", "gemini", "openrouter", "anthropic", "ollama", "deepseek", "gemini_cli"]
    return JSONResponse({p: has_credentials(p) for p in providers})


@router.get("/stats/claude-usage")
async def api_claude_usage():
    stats = get_claude_usage_stats()
    week_cost = stats.get("week_cost", 0.0)
    week_pct = min(week_cost / CLAUDE_WEEKLY_BUDGET * 100, 100) if CLAUDE_WEEKLY_BUDGET else 0.0
    return JSONResponse({
        "session_cost": stats.get("session_cost", 0.0),
        "session_count": stats.get("session_count", 0),
        "week_cost": week_cost,
        "week_count": stats.get("week_count", 0),
        "week_pct": week_pct,
        "week_budget": CLAUDE_WEEKLY_BUDGET,
    })


@router.get("/entroly/status")
async def api_entroly_status():
    return JSONResponse({"enabled": False, "available": False, "quality": 0.0, "token_budget": 0})


@router.get("/stacks")
async def api_stacks_list():
    from ...stacks import STACKS, get_active_stack, get_custom_overrides, AVAILABLE_MODELS
    from ...providers.ollama import is_available as ollama_available
    active = get_active_stack()
    custom = get_custom_overrides()
    preset_cfg = {k: v for k, v in STACKS[active].items() if k != "description"}
    effective = {agent: dict(cfg) for agent, cfg in preset_cfg.items()}
    for agent, cfg in custom.items():
        if agent in effective:
            effective[agent] = {**effective[agent], **cfg}
    return JSONResponse({
        "active": active,
        "ollama_available": ollama_available(),
        "custom_overrides": custom,
        "effective": effective,
        "available_models": AVAILABLE_MODELS,
        "stacks": {
            name: {
                "description": cfg.get("description", name),
                "agents": {k: v for k, v in cfg.items() if k != "description"},
            }
            for name, cfg in STACKS.items()
        },
    })


class StackPayload(BaseModel):
    stack: str


@router.post("/session/stack")
async def api_session_set_stack(payload: StackPayload):
    from ...stacks import set_active_stack, STACKS
    if payload.stack not in STACKS:
        return JSONResponse({"error": f"Unknown stack: {payload.stack}"}, status_code=400)
    set_active_stack(payload.stack)
    return JSONResponse({"ok": True, "stack": payload.stack})


class AgentOverridesPayload(BaseModel):
    overrides: dict


@router.post("/session/agent-overrides")
async def api_session_agent_overrides(payload: AgentOverridesPayload):
    from ...stacks import set_custom_overrides
    set_custom_overrides(payload.overrides)
    return JSONResponse({"ok": True})
