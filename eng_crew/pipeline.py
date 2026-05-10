"""Top-level pipeline: wires LangGraph StateGraph and manages run lifecycle."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

from eng_crew import git_skill, hitl as hitl_mod, tracker
from eng_crew.agents.architect import ArchitectAgent
from eng_crew.agents.dispatcher import DispatcherAgent
from eng_crew.agents.executor import ExecutorAgent
from eng_crew.agents.orchestrator import OrchestratorAgent
from eng_crew.agents.reviewer import ReviewerAgent
from eng_crew.config import Settings
from eng_crew.project_context import load_project_context
from eng_crew.state import TeamState

log = logging.getLogger(__name__)


def _build_graph(settings: Settings) -> Any:
    orchestrator = OrchestratorAgent(settings)
    architect = ArchitectAgent(settings)
    dispatcher = DispatcherAgent(settings)
    reviewer = ReviewerAgent(settings)
    executor = ExecutorAgent(settings)

    def orchestrator_node(state: TeamState) -> dict:
        return orchestrator.run(state)

    def architect_node(state: TeamState) -> dict:
        return architect.decompose(state)

    def hitl_gate_node(state: TeamState) -> dict:
        subtasks = state["subtasks"]
        run_id = state["run_id"]
        if settings.require_approval:
            _, approved, _feedback = hitl_mod.dashboard_prompt(subtasks, run_id)
        else:
            _, approved, _feedback = hitl_mod.prompt_user(subtasks, run_id, ci_mode=True)
        return {
            "plan_approved": approved,
            "_next": "dispatcher" if approved else "rejected",
        }

    def dispatcher_node(state: TeamState) -> dict:
        return dispatcher.dispatch(state)

    def reviewer_node(state: TeamState) -> dict:
        return reviewer.review(state)

    def executor_node(state: TeamState) -> dict:
        return executor.execute(state)

    def _route_orchestrator(state: TeamState) -> str:
        return state.get("_next") or "done"

    def _route_hitl(state: TeamState) -> str:
        return state.get("_next") or "rejected"

    def _route_reviewer(state: TeamState) -> str:
        return state.get("_next") or "done"

    graph = StateGraph(TeamState)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("architect", architect_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("dispatcher", dispatcher_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("executor", executor_node)

    graph.set_entry_point("orchestrator")

    graph.add_conditional_edges(
        "orchestrator",
        _route_orchestrator,
        {
            "architect": "architect",
            "dispatcher": "dispatcher",
            "done": END,
        },
    )
    graph.add_edge("architect", "hitl_gate")
    graph.add_conditional_edges(
        "hitl_gate",
        _route_hitl,
        {
            "dispatcher": "dispatcher",
            "rejected": END,
        },
    )
    graph.add_edge("dispatcher", "reviewer")
    graph.add_conditional_edges(
        "reviewer",
        _route_reviewer,
        {
            "executor": "executor",
            "dispatcher": "dispatcher",
            "orchestrator": "orchestrator",
            "done": END,
        },
    )
    graph.add_edge("executor", "orchestrator")

    return graph.compile()


def run_pipeline(task: str, project_path: str, settings: Settings) -> TeamState:
    """Create git branch, build graph, run agents, finalize tracker."""
    run_id = tracker.create_run(task, project_path)
    tracker.update_run_status(run_id, "running")

    branch: str | None = None
    try:
        branch = git_skill.ensure_branch(project_path, settings.branch_prefix, task[:48])
    except Exception as exc:
        log.warning("git branch creation failed: %s", exc)

    ctx = load_project_context(project_path)
    project_context = ctx.render()
    claude_md_path = str(Path(project_path).expanduser().resolve() / "CLAUDE.md")

    initial_state: TeamState = {
        "run_id": run_id,
        "raw_task": task,
        "project_path": project_path,
        "claude_md_path": claude_md_path,
        "project_context": project_context,
        "git_branch": branch,
        "subtasks": [],
        "current_subtask_idx": 0,
        "active_subtask_ids": [],
        "completed_subtask_ids": [],
        "execution_results": [],
        "final_summary": None,
        "plan_approved": False,
        "orchestrator_loops": 0,
        "_next": None,
        "_review_decision": None,
        "_skip_action": None,
        "_design_text": None,
        "_critique_text": None,
        "critic_replan_count": 0,
        "test_fix_count": 0,
        "failed_subtask_count": 0,
        "plan_id": None,
        "_test_failed": None,
        "clarification_requested": None,
        "complexity_tier": None,
    }

    compiled = _build_graph(settings)
    try:
        final_state: TeamState = compiled.invoke(initial_state)
        summary = final_state.get("final_summary") or "Pipeline completed."
        tracker.finish_run(run_id, status="completed", final_summary=summary)
        return final_state
    except Exception as exc:
        log.exception("Pipeline failed: %s", exc)
        tracker.finish_run(run_id, status="failed", final_summary=str(exc))
        raise
