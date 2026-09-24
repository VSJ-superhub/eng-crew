# eng-crew

A public, open-source AI engineering team that autonomously decomposes, codes, reviews, and executes software tasks on any project.

## Goal

This is a generalized, pip-installable version of a private internal tool. It must work out-of-the-box for any developer on any OS with minimal setup.

## Key Requirements

- No hardcoded paths — all config via `.env` or `config.yaml`
- Cross-platform (Windows, macOS, Linux)
- Clean install: `pip install eng-crew` or `docker compose up`
- Multi-agent pipeline: architect → critic → HITL approval → specialist coders → reviewer → executor
- The unit of work is a **ticket** (`eng_crew/tickets.py`), not a sprint. Tickets
  carry acceptance criteria and move along a fixed lifecycle: backlog → ready →
  in_progress → in_review → in_qa → done/rejected → shipped. A ticket cannot
  reach `ready` without acceptance criteria, every status change is recorded in
  `ticket_events` with the actor who made it, and each run links back to the
  ticket it attempts.
- There are no sprints. Tickets form one continuous queue ordered by priority,
  bounded by a WIP limit (`ENG_CREW_WIP_LIMIT`, default 3) on how many are in
  flight (in_progress/in_review/in_qa) per project.
- Planning a project creates tickets (`eng_crew/planning.py`); it does not launch
  a build of the goal text. Running queued work builds the actual tickets — the
  task handed to the pipeline is the ticket's title, description, and acceptance
  criteria.
- Agent prompts live in `.claude/skills/<name>/SKILL.md` (frontmatter + template),
  loaded by `eng_crew/prompts.py` — edit a prompt without touching agent code.
- Runs execute in their own git worktree and branch (`worktree_isolation`, on by
  default), so a run never stashes your changes or moves the branch you are on.
- Pause is honoured between graph nodes (an agent turn is one CLI call and cannot
  be interrupted mid-flight); resume releases the same run rather than restarting it.
- A run commits its output in the worktree after verification, so the result is a
  reviewable diff on a branch rather than loose uncommitted files.
- Old worktrees are pruned at the start of a run, and every clean, fully merged
  worktree (this run's own included) is swept once a run finishes — only ones
  that are clean and fully merged, since an uncommitted worktree is a run's
  output. `eng-crew worktrees` lists them; `eng-crew prune-worktrees --dry-run`
  shows what would go.
- A run's branch forks from the project checkout's HEAD by default. Pass a base
  (`eng-crew run --base <branch>`, or `base_branch` to `run_task`) to stack the
  run on an earlier run's branch instead.
- Every execution tier (simple / single-agent / full graph) exits through a deterministic
  verification gate (`eng_crew/verify.py`) that runs the project's own tests and build.
  A run that leaves the tree broken is recorded as `failed`, not `completed`.
- Dashboard at configurable port (default 9000)
- Supports multiple LLM providers: Anthropic Claude CLI, OpenRouter, Gemini
