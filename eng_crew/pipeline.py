"""Top-level pipeline: wires LangGraph StateGraph and manages run lifecycle."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langgraph.graph import END, StateGraph

from eng_crew import git_skill, hitl as hitl_mod, tracker
from eng_crew.agents.architect import ArchitectAgent
from eng_crew.agents.complexity_classifier import ComplexityClassifierAgent
from eng_crew.agents.dispatcher import DispatcherAgent
from eng_crew.agents.executor import ExecutorAgent
from eng_crew.agents.orchestrator import OrchestratorAgent
from eng_crew.agents.reviewer import ReviewerAgent
from eng_crew.agents.simple_executor import SimpleExecutorAgent
from eng_crew.agents.verifier import VerifierAgent
from eng_crew.agents.single_agent import SingleAgentEngineer
from eng_crew.config import Settings
from eng_crew.project_context import load_project_context
from eng_crew.state import TeamState

log = logging.getLogger(__name__)


def _build_graph(settings: Settings) -> Any:
    classifier = ComplexityClassifierAgent(settings)
    orchestrator = OrchestratorAgent(settings)
    architect = ArchitectAgent(settings)
    dispatcher = DispatcherAgent(settings)
    reviewer = ReviewerAgent(settings)
    executor = ExecutorAgent(settings)
    simple_executor = SimpleExecutorAgent(settings)
    single_agent = SingleAgentEngineer(settings)
    verifier = VerifierAgent(settings)

    def classify_node(state: TeamState) -> dict:
        return classifier.run(state)

    def simple_execute_node(state: TeamState) -> dict:
        return simple_executor.run(state)

    def single_execute_node(state: TeamState) -> dict:
        return single_agent.run(state)

    def verify_node(state: TeamState) -> dict:
        return verifier.run(state)

    def _route_classify(state: TeamState) -> str:
        return state.get("_next") or "full"

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
    graph.add_node("classify", classify_node)
    graph.add_node("simple_execute", simple_execute_node)
    graph.add_node("single_execute", single_execute_node)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("architect", architect_node)
    graph.add_node("hitl_gate", hitl_gate_node)
    graph.add_node("dispatcher", dispatcher_node)
    graph.add_node("reviewer", reviewer_node)
    graph.add_node("executor", executor_node)
    graph.add_node("verify", verify_node)

    graph.set_entry_point("classify")
    graph.add_conditional_edges("classify", _route_classify, {
        "simple": "simple_execute",
        "single": "single_execute",
        "full":   "orchestrator",
    })
    graph.add_edge("simple_execute", "verify")
    graph.add_edge("single_execute", "verify")
    graph.add_edge("verify", END)

    graph.add_conditional_edges(
        "orchestrator",
        _route_orchestrator,
        {
            "architect": "architect",
            "dispatcher": "dispatcher",
            "done": "verify",
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


def run_pipeline(
    task: str,
    project_path: str,
    settings: Settings,
    *,
    run_id: int | None = None,
) -> TeamState:
    """Create git branch, build graph, run agents, finalize tracker.

    If ``run_id`` is provided, the caller has already created the run row
    (e.g. the Discord bot, which pre-creates the run so it can poll the DB
    for the HITL approval gate). Otherwise a new run is created here.
    """
    # Callers (CLI/typer with resolve_path, dashboard) may pass a Path object.
    # Coerce once at the boundary so tracker binds, state, and git all see a str.
    project_path = str(project_path)
    if run_id is None:
        run_id = tracker.create_run(task, project_path)
    tracker.update_run_status(run_id, "running")

    branch: str | None = None
    worktree_path: str | None = None
    main_project_path = project_path
    # work_path is where the agents actually edit. With worktree isolation that
    # is a private checkout; without it, the project itself.
    work_path = project_path

    if settings.worktree_isolation and git_skill.is_git_repo(project_path):
        if settings.worktree_auto_prune:
            # Prune before creating this run's worktree, never after — the run's
            # own output must not be a pruning candidate.
            try:
                pruned = git_skill.prune_worktrees(
                    project_path,
                    keep_last=settings.worktree_keep_last,
                    max_age_days=settings.worktree_retention_days,
                )
                if pruned:
                    log.info("pruned %d stale worktree(s)", len(pruned))
            except Exception as exc:
                log.warning("worktree prune failed: %s", exc)
        try:
            branch = git_skill.make_branch_name(settings.branch_prefix, task[:48])
            wt = git_skill.create_worktree(
                project_path, branch,
                worktree_dir=settings.worktree_dir or None,
            )
            worktree_path = str(wt)
            work_path = worktree_path
            names = [n.strip() for n in settings.worktree_link_dirs.split(",") if n.strip()]
            linked = git_skill.link_into_worktree(
                git_skill.repo_root(project_path), wt, names
            )
            log.info(
                "worktree %s on branch %s (linked: %s)",
                wt, branch, ", ".join(linked) or "nothing",
            )
        except Exception as exc:
            # Isolation is a safety improvement, not a precondition — fall back
            # to the in-place branch rather than failing the run.
            log.warning("worktree creation failed (%s) — falling back to in-place branch", exc)
            branch, worktree_path, work_path = None, None, project_path

    if branch is None:
        try:
            branch = git_skill.ensure_branch(project_path, settings.branch_prefix, task[:48])
        except Exception as exc:
            log.warning("git branch creation failed: %s", exc)

    ctx = load_project_context(work_path)
    project_context = ctx.render()
    claude_md_path = str(Path(work_path).expanduser().resolve() / "CLAUDE.md")

    initial_state: TeamState = {
        "run_id": run_id,
        "raw_task": task,
        "project_path": work_path,
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
        "verification_passed": None,
        "verification_summary": None,
        "verification_unverified": None,
        "verify_fix_count": 0,
        "cli_session_id": None,
        "worktree_path": worktree_path,
        "main_project_path": main_project_path,
    }

    compiled = _build_graph(settings)
    try:
        final_state: TeamState = compiled.invoke(initial_state)
        summary = final_state.get("final_summary") or "Pipeline completed."
        if worktree_path:
            summary = f"{summary}\n\nWorktree: {worktree_path} (branch {branch})"
        # The verification gate, not the absence of an exception, decides success.
        verified = final_state.get("verification_passed")
        status = "failed" if verified is False else "completed"
        if verified is False:
            log.warning(
                "Run %s failed verification: %s",
                run_id, final_state.get("verification_summary"),
            )
        tracker.finish_run(run_id, status=status, final_summary=summary)
        return final_state
    except Exception as exc:
        log.exception("Pipeline failed: %s", exc)
        tracker.finish_run(run_id, status="failed", final_summary=str(exc))
        raise
