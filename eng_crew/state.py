from typing import TypedDict, List, Optional


class Subtask(TypedDict):
    id: str
    description: str
    target_files: List[str]
    agent_type: Optional[str]
    patch: Optional[str]
    review_passed: Optional[bool]
    review_feedback: Optional[str]
    retry_count: int
    dependencies: List[str]
    human_comment: Optional[str]
    clarification_question: Optional[str]
    clarification_response: Optional[str]
    status: str
    critic_feedback: Optional[str]


class TeamState(TypedDict):
    run_id: Optional[int]
    raw_task: str
    project_path: str
    claude_md_path: str
    project_context: str
    git_branch: Optional[str]
    subtasks: List[Subtask]
    current_subtask_idx: int
    active_subtask_ids: List[str]
    completed_subtask_ids: List[str]
    execution_results: List[str]
    final_summary: Optional[str]
    plan_approved: bool
    orchestrator_loops: int
    _next: Optional[str]
    _review_decision: Optional[str]
    _skip_action: Optional[str]
    _design_text: Optional[str]
    _critique_text: Optional[str]
    critic_replan_count: int
    test_fix_count: int
    failed_subtask_count: int
    plan_id: Optional[int]
    _test_failed: Optional[bool]
    clarification_requested: Optional[bool]
    complexity_tier: Optional[str]
