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
- The Manager (`eng_crew/manager.py`) is the product manager: an Ideate
  conversation (dashboard or Discord `/ideate`) ends in a ticket proposal — title,
  description, acceptance criteria — and accepting it files a backlog ticket
  credited to `manager`. It starts no run. A proposal that arrived without
  acceptance criteria still becomes a ticket, but the surface says so, because the
  lifecycle will refuse to make that ticket `ready`. Approving the ticket to
  `ready` is what releases it into the ticket queue to be built.
- When a ticket's run lands, an independent reviewer (`eng_crew/review.py`)
  checks the run's diff against the ticket's acceptance criteria: one cheap model
  call on the `ticket_reviewer` role, never a CLI provider (it must not be able
  to edit the code it judges), with its cost recorded against the run. The
  verdict and its reasons go into `ticket_events`. An approved ticket whose run
  also passed the verification gate moves in_review → in_qa → done, credited to
  `reviewer` and `qa`. A changes-requested verdict leaves the ticket in
  in_review with the reasons attached and starts no new build. `done → shipped`
  is refused for every eng-crew actor (`tickets.ROBOTS`) — shipping is a human
  action.
- Every ticket carries a spending budget (`eng_crew/budget.py`) — the
  configured default (`ENG_CREW_TICKET_BUDGET_USD`, $15) unless the ticket
  overrides it. A ticket's spend is the total of every run charged to it,
  repair passes and reviews included. Because a model call cannot be
  interrupted mid-flight, the budget is enforced where a *new* paid step would
  start: entering `in_progress` (a build), and before a repair pass or a
  review. The step already running finishes; the next one does not begin. A
  stopped ticket records why in `ticket_events` and waits — nothing retries it.
  Raising the budget (`eng-crew budget <id> <amount>`, or the dashboard) is what
  releases it. A run with no ticket is never budget-checked.
- Shipping a ticket ends in a retro (`eng_crew/retro.py`). `done → shipped`
  writes a short note into the existing lessons store (the `memories` table)
  built from the ticket's own history — its runs, what it spent against its
  budget, the review verdicts and QA outcome, how many builds it took. The
  retro is assembled from recorded facts, not a model call, so it costs
  nothing; a retro that fails never unships the ticket. Starting a later build
  feeds the relevant past retros (ranked by overlap with the task, this
  project's first) into the builder's prompt, so the lessons are read by the
  agent that could act on them rather than sitting in a table.
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
- When a task carries acceptance criteria (tickets do), the single-agent builder
  works test-first: it writes tests encoding the criteria, confirms they fail,
  then implements until they pass. The gate's red phase then replays those tests
  against pre-change code and is strict by default — a new test that passes
  without the change fails the run. A run that adds no tests skips the phase,
  unless the task carried acceptance criteria and the project has a test suite:
  then adding no tests fails the run too, since test-first never happened.
- Dashboard at configurable port (default 9000)
- Supports multiple LLM providers: Anthropic Claude CLI, OpenRouter, Gemini
