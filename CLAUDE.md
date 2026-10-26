# eng-crew

A public, open-source AI engineering team that autonomously decomposes, codes, reviews, and executes software tasks on any project.

## Goal

This is a generalized, pip-installable version of a private internal tool. It must work out-of-the-box for any developer on any OS with minimal setup.

## Key Requirements

- No hardcoded paths — all config via `.env` or `config.yaml`. A source checkout
  uses the `.env` and `.eng-crew/` at its root; an installed package reads
  `~/.eng-crew/.env` and a `.env` in the working directory (which wins) and
  keeps its data under `~/.eng-crew` (`ENG_CREW_HOME`), never in site-packages
  (`eng_crew/config.py`: `is_source_checkout`, `env_files`, `default_data_dir`).
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
- A ticket can record the tickets it waits on (`depends_on`, parsed and
  enforced in `eng_crew/tickets.py`). A prerequisite counts as met once it is
  `done` or `shipped`. The planner emits dependencies between the tickets it
  writes, the queue passes over a `ready` ticket whose prerequisites are
  unfinished and picks by priority among the rest, and every surface that can
  start a build — the CLI, the dashboard, Discord `/build` — refuses a blocked
  one naming what is unfinished and leaves the ticket where it was. A
  dependency that would close a cycle is refused rather than stored. A ticket
  with no dependencies behaves exactly as it always did.
- A dependent ticket's run starts from its prerequisites' work
  (`eng_crew/basing.py`). A prerequisite counts as met when it is `done`, but
  its work sits on its run's branch until somebody merges it — so when a
  prerequisite's branch is not yet contained in the project checkout's HEAD,
  the run forks from that branch instead of from HEAD — as long as that branch
  already contains HEAD. With several such branches, or one that HEAD has
  moved on from, it forks from a branch that merges them onto HEAD; a
  prerequisite branch another one already contains is carried by it, not
  merged twice. If they cannot be combined the build is refused before it
  starts, naming the conflicting prerequisites and whether they conflict with
  each other or with HEAD, and the ticket stays where it was. That refusal
  is not a dead end: `eng-crew resolve <id>`, or the card's Resolve conflicts
  button, has an agent merge the prerequisites onto HEAD in a worktree on
  `eng-crew/resolved/ticket-<id>`, fix the conflicts and pass the project's
  checks there (`eng_crew/resolve.py`). It is charged to the ticket's budget,
  never touches the checkout and starts no build; the next build forks from
  that branch — merged onto HEAD if HEAD has moved on since — and a resolution
  that leaves a conflict or breaks the checks is thrown away. The branch a
  run was based on and why are recorded on the run (`runs.base_branch`,
  `runs.base_reason`) and shown with it. Every surface gets this, because
  `eng-crew ticket <id>` — what the CLI, the dashboard, Discord `/build` and a
  queue pull all launch — is where it is resolved; an explicit base from the
  caller (`--base`, `base_branch`) still wins.
- A ticket carries an **area** (`eng_crew/tickets.py`): a short per-project
  label — Engine, Website, Admin — saying which part of the product the work
  belongs to. The planner decides a small set of them for the project and gives
  every ticket one of those names, reusing the spelling already in play rather
  than inventing a label per ticket. A ticket with no area keeps working and
  shows under "Unassigned". Changing one is recorded in `ticket_events` with the
  actor, like a status change, and can be done from the dashboard, Discord
  (`/area`) or the CLI (`eng-crew area <id> <name>`).
- The **Roadmap** (`tickets.roadmap`) is what areas are for: the same tickets
  grouped by area instead of status, ordered so a prerequisite is always above
  whatever waits on it, each one marked done, ready to start, in flight, or
  blocked with the unfinished prerequisites named. One function serves all three
  surfaces — the dashboard project page's Roadmap tab, Discord's `/roadmap
  <project>` (bounded in length like `/backlog`) and `eng-crew roadmap` — so
  they cannot disagree about what is blocked. The discussion agent reads the
  tracker grouped by area too, when areas are set.
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
  call on the `ticket_reviewer` role, with its cost recorded against the run. It
  must not be able to edit the code it judges, so a CLI-only setup reviews
  through the Claude CLI with every file and shell tool disabled. The diff is
  measured against the branch the run was built on (`runs.base_branch`), so a
  run stacked on an unmerged prerequisite is not judged on the prerequisite's
  code, and is cut at `ENG_CREW_TICKET_REVIEW_MAX_DIFF_CHARS` (120000). The
  verdict and its reasons go into `ticket_events`. An approved ticket whose run
  also passed the verification gate moves in_review → in_qa → done, credited to
  `reviewer` and `qa` — unless the diff was truncated: an approval that did not
  see every changed file says how many it saw and which it did not, and leaves
  the ticket in in_review for a person. A changes-requested verdict leaves the
  ticket in in_review with the reasons attached and starts no new build. A
  review that could not run at all — no API key, a provider error, an empty
  reply, a spent budget, or a reviewer answering `UNABLE_TO_JUDGE` because a
  criterion turns on a file the truncated diff never showed it — is not a
  verdict: the ticket stays in_review with a "review unavailable" event saying
  why, never `changes_requested`, and is *waiting on a review*
  (`review.awaiting_review`) until one gives a verdict.
  `eng-crew review <id>` and the dashboard's Run review button run that review;
  a ticket with a verdict already is not reviewed again. `done → shipped` is
  refused for every eng-crew actor (`tickets.ROBOTS`) — shipping is a human
  action.
- Every ticket carries a spending budget (`eng_crew/budget.py`) — the
  configured default (`ENG_CREW_TICKET_BUDGET_USD`, $15) unless the ticket
  overrides it. A ticket's spend is the total of every run charged to it,
  repair passes and reviews included. Because a model call cannot be
  interrupted mid-flight, the budget is enforced where a *new* paid step would
  start: entering `in_progress` (a build), and before a repair pass or a
  review. The step already running finishes; the next one does not begin. A
  repair pass is also capped at what the ticket has left (`--max-budget-usd`
  on the Claude CLI session), so one pass cannot carry a ticket far past its
  budget. A stopped ticket records why in `ticket_events` and waits — nothing
  retries it. Raising the budget (`eng-crew budget <id> <amount>`, or the
  dashboard) is what releases it: a review the budget stopped runs then, and
  the next build checks the new figure. Raising it starts no build. A run with
  no ticket is never budget-checked.
- Shipping a ticket ends in a retro (`eng_crew/retro.py`). `done → shipped`
  writes a short note into the existing lessons store (the `memories` table)
  built from the ticket's own history — its runs, what it spent against its
  budget, the review verdicts and QA outcome, how many builds it took. The
  retro is assembled from recorded facts, not a model call, so it costs
  nothing; a retro that fails never unships the ticket. Starting a later build
  feeds the relevant past retros (ranked by overlap with the task, this
  project's first) into the builder's prompt, so the lessons are read by the
  agent that could act on them rather than sitting in a table.
- Dispatching a free-text task (`run_task`, the MCP tool) files a **ticket** and
  builds that (`eng_crew/dispatch.py`), so the build gets the independent review,
  the spending cap and the retro a ticket's build gets. Acceptance criteria come
  from the caller, or the Manager drafts them from the task and the real codebase
  when none are given — a dispatch with no definition of done files a ticket and
  starts nothing. Dispatching is the human's approval, so it is what moves the
  ticket to `ready`. Over the WIP limit the ticket is filed and readied but no
  build starts, and the caller is told it is queued.
- Building a ticket that already exists is the other way in
  (`dispatch.build_ticket`), for a surface whose user is looking at a queue
  rather than describing new work — Discord's `/queue <project>` and `/build
  <ticket_id>`. It files nothing: approving a `backlog` ticket to `ready` is
  credited to the person who asked, and everything after that is the path the
  dashboard and the CLI take, under the same lifecycle, WIP limit and budget.
- The tracker is readable from Discord as well as the dashboard: `/backlog
  <project>` renders the WIP count, a tally per status and the tickets grouped
  in lifecycle order, and `/ticket <id>` renders one ticket in full — criteria,
  spend, the latest review verdict, its runs and its history. Both are
  read-only; neither moves a ticket or starts a run.
- The Discord bot's own dependency and credentials are checked before it is
  launched, and its output is kept in `<data dir>/logs/discord_bot.log`
  (`eng_crew/discord_service.py`), so a bot that dies at startup says why
  instead of being reported as "started" and then, silently, "stopped".
  `eng-crew discord [--check]` runs it in the foreground with the reason
  printed at you.
- Every agent CLI call eng-crew makes — builders, repair passes, the ticket
  reviewer, the manager, the discussion agent — runs with an explicit MCP
  configuration and `--strict-mcp-config` (`eng_crew/mcp_policy.py`), so it
  ignores the user's global and project MCP servers rather than inheriting
  whatever can mail, trade or dispatch on their behalf. eng-crew's own server is
  never among an agent's servers, however it is spelled: a run may not dispatch,
  plan or build other eng-crew work from inside itself. What agents do get is
  `ENG_CREW_AGENT_MCP_SERVERS` — inline JSON or a path to a JSON file, empty by
  default — and a sandboxed call (the reviewer) gets none at all.
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
- The **Entroly** context optimizer (`eng_crew/entroly_integration.py`) is an
  optional extra, off by default (`ENTROLY_ENABLED`, with `ENTROLY_QUALITY` and
  `ENTROLY_TOKEN_BUDGET`). Enabled and installed, a run refreshes the project's
  Entroly index before the agents start, the project context they are handed is
  compressed to the token budget, and `entroly` is added to the MCP servers the
  CLI gives agents (`providers/claude_cli._extra_mcp_servers`, so a sandboxed
  session still gets none). Off, or with the CLI missing, eng-crew behaves
  exactly as it does without it, and every entry point falls back to the
  uncompressed context rather than failing a run. `/api/entroly/status` and the
  dashboard card report the real state.
- Dashboard at configurable port (default 9000)
- Supports multiple LLM providers: Anthropic Claude CLI, OpenRouter, Gemini
