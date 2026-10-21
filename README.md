# eng-crew

An autonomous AI engineering team that decomposes, codes, reviews, and executes software tasks on any project.

The pipeline runs: **architect → critic → approval → coders → reviewer → executor**

## Quickstart

### 1. Install

```bash
pip install eng-crew
```

Or run from source:

```bash
git clone https://github.com/VSJ-superhub/eng-crew
cd eng-crew
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
```

**Where `.env` and the data live.** Running from a clone of this repository,
eng-crew uses the `.env` and `.eng-crew/` directory beside `eng_crew/` — if you
already have those, nothing moves. Installed with `pip install eng-crew`, there
is no such directory to use (it would be `site-packages`), so eng-crew reads
`~/.eng-crew/.env` and then a `.env` in the directory you run it from, which
wins; and it keeps its database, logs and run artifacts under `~/.eng-crew`.
Point that elsewhere with `ENG_CREW_HOME`, or set `ENG_CREW_DATA_DIR` to put
only the data somewhere specific.

**Free option — Claude CLI (no API key needed):**

```dotenv
ENG_CREW_PROVIDER=claude_cli
```

Requires [Claude Code CLI](https://claude.ai/code) installed and authenticated:

```bash
npm install -g @anthropic-ai/claude-code
claude   # follow the login prompt once
```

**Free option — Gemini CLI (no API key needed):**

```dotenv
ENG_CREW_PROVIDER=gemini_cli
```

Requires [Gemini CLI](https://github.com/google-gemini/gemini-cli) installed and authenticated:

```bash
npm install -g @google/gemini-cli
gemini   # follow the Google login prompt once
```

**Paid API options:**

```dotenv
# Anthropic API
ENG_CREW_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

# Google Gemini API
ENG_CREW_PROVIDER=gemini
GEMINI_API_KEY=...

# OpenRouter (access many models via one key)
ENG_CREW_PROVIDER=openrouter
OPENROUTER_API_KEY=...
```

### 3. Run a task

```bash
eng-crew run --project /path/to/your/project "Add pagination to the users table — 20 rows per page"
```

The dashboard opens automatically at **http://localhost:9000** where you can approve the plan and watch progress live.

### 4. Approve and go

The pipeline pauses for human-in-the-loop approval after the architect produces a plan. Visit the dashboard, review the subtasks, then click **Approve** to start coding.

To skip approval (fully autonomous mode):

```bash
eng-crew run --no-approval --project /path/to/project "Fix the login bug"
```

## Docker

```bash
cp .env.example .env
# edit .env with your API key
docker compose up
```

Dashboard: **http://localhost:9000**

## Configuration reference

All settings are environment variables (see `.env.example` for the full list).

| Variable | Default | Description |
|---|---|---|
| `ENG_CREW_PROVIDER` | `claude_cli` | LLM provider: `claude_cli`, `gemini_cli`, `anthropic`, `gemini`, `openrouter` |
| `ANTHROPIC_API_KEY` | — | Anthropic API key (only needed for `anthropic` provider) |
| `GEMINI_API_KEY` | — | Google Gemini API key (only needed for `gemini` provider) |
| `OPENROUTER_API_KEY` | — | OpenRouter API key (only needed for `openrouter` provider) |
| `ENG_CREW_ARCHITECT_MODEL` | `claude-sonnet-4-6` | Model for architect agent |
| `ENG_CREW_CODER_MODEL` | `claude-sonnet-4-6` | Model for coder agents |
| `ENG_CREW_REVIEWER_MODEL` | `claude-sonnet-4-6` | Model for reviewer agent |
| `ENG_CREW_BUDGET_USD` | `5.00` | Max spend per run in USD (0 = unlimited) |
| `ENG_CREW_DASHBOARD_PORT` | `9000` | Dashboard port |
| `ENG_CREW_REQUIRE_APPROVAL` | `true` | Pause for human approval before coding |
| `ENG_CREW_WIP_LIMIT` | `3` | Max tickets in flight per project at once |
| `ENG_CREW_CODER_PARALLELISM` | `2` | Number of parallel coder agents (1–8) |
| `ENG_CREW_DATA_DIR` | `.eng-crew` in a checkout, else `~/.eng-crew` | Directory for the database, logs, and run artifacts |
| `ENG_CREW_HOME` | `~/.eng-crew` | Where an installed eng-crew looks for `.env` and keeps data |
| `ENG_CREW_TICKET_REVIEWER_PROVIDER` | from the stack | Provider for the independent ticket review |
| `ENG_CREW_TICKET_REVIEW_ENABLED` | `true` | Set `false` to skip the independent ticket review |

## Tickets

The unit of work is a **ticket**, not a sprint. A ticket carries acceptance
criteria and moves along a fixed lifecycle:

```
backlog -> ready -> in_progress -> in_review -> in_qa -> done -> shipped
                                                    \-> rejected -/
```

- **Acceptance criteria are required to reach `ready`.** "Ready" means an agent
  could pick the ticket up and know when it is finished, so a ticket with no
  definition of done cannot get there.
- **Moving a ticket to `ready` is the approval step.** The planner only
  proposes: its tickets land in `backlog`, and a human promotes the ones worth
  building.
- **Every status change is recorded** with who made it, so a ticket's history
  shows how it got where it is (`GET /api/tickets/<id>/events`).
- **Runs are linked to the ticket they attempt**, and a run is handed the
  ticket's own title, description, and acceptance criteria.
- **Acceptance criteria make the build test-first.** The builder writes tests
  that encode the criteria and confirms they fail before implementing anything,
  and the verification gate replays those tests against the pre-change code to
  check they actually discriminate. A test that would have passed anyway fails
  the run (`ENG_CREW_VERIFICATION_RED_PHASE=warn` to only report it, `off` to
  disable).
- **A landed run is reviewed by something that is not the builder.** One cheap
  model call reads the run's diff against the ticket's acceptance criteria and
  records `approve` or `changes_requested` with reasons in the ticket's history;
  its cost lands on the run. It has no file tools: when your only provider is a
  CLI one, the review runs through the Claude CLI with every file and shell tool
  disabled, so it cannot edit the code it judges. An approved ticket whose run
  also passed the verification gate moves through `in_qa` to `done`, credited to
  `reviewer` and `qa`. Changes requested leaves the ticket in `in_review` with
  the reasons and starts no new build — moving it back to `in_progress` is your
  call (`ENG_CREW_TICKET_REVIEWER_MODEL`, `ENG_CREW_TICKET_REVIEW_ENABLED=false`).
- **A review that could not run is not a rejection.** No API key for the
  configured reviewer, a provider that errors, an empty reply — none of that is
  evidence about the work. The ticket stays in `in_review` and its history says
  the review was **unavailable** and why, rather than recording a verdict
  nothing actually reached.
- **`done -> shipped` is a human action.** No eng-crew actor can make that move.
- **There are no sprints.** Tickets form one continuous queue ordered by
  priority; `ENG_CREW_WIP_LIMIT` bounds how many are in flight at once. Nothing
  enters `in_progress` past the limit.

Planning a project produces tickets — it does not build the goal text:

```bash
# On the dashboard: open a project -> "Plan project"
eng-crew queue  /path/to/project        # see the queue and the WIP count
eng-crew ticket 42                      # build one ticket
```

## Discord

The bot is an optional surface for the same ticket queue, so a plan made at a
desk can be worked from a phone.

```bash
pip install 'eng-crew[discord]'         # the bot's one extra dependency
# put DISCORD_BOT_TOKEN=... in your .env, then:
eng-crew discord --check                # will it start? says what is missing
eng-crew discord                        # run it in the foreground
```

| Command | What it does |
|---|---|
| `/projects` | Project aliases — every project eng-crew has registered, plus any in `bot_config.json` |
| `/queue <project>` | That project's ticket queue: id, priority, status, title, whether it has acceptance criteria, spend against budget, and WIP in flight — the same data `eng-crew queue` prints |
| `/backlog <project> [status]` | The whole tracker as an embed: WIP, a tally per status, and the tickets grouped in lifecycle order — optionally one status only |
| `/ticket <id>` | One ticket in full: description, acceptance criteria, spend, the latest review verdict, its runs and its history |
| `/build <ticket_id>` | Build a ticket that already exists. A `backlog` ticket is approved to `ready` in your name first; nothing new is filed |
| `/task <project> <task>` | File *new* work as a ticket and build it |
| `/ideate <project>` | Talk an idea through with the Manager; it ends in a ticket proposal |
| `/status` | Recent and active runs |

`/build` is bound by exactly what the dashboard and the CLI are bound by: a
ticket with no acceptance criteria is refused, a ticket that has spent its
budget is refused with the figure, and a project at its WIP limit leaves the
ticket `ready` in the queue and starts nothing.

`start_services` keeps the bot's output in `<data dir>/logs/discord_bot.log`, so
a bot that dies at startup leaves its reason there and in `services_status()`.

## CLI reference

```
eng-crew run      --project PATH  "task description"   Run a task
eng-crew ticket   TICKET_ID                            Build one ticket
eng-crew queue    PATH                                 Show the ticket queue and WIP
eng-crew plans    PATH                                 Show run plans for a project
eng-crew resume   RUN_ID                               Resume an interrupted run
eng-crew status                                         Show recent runs
eng-crew dashboard                                      Open the dashboard
eng-crew discord  [--check]                             Run the Discord bot, or say why it will not start
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'eng_crew'`**
Run `pip install -e .` from the project root, or `pip install eng-crew` from PyPI.

**Dashboard not opening**
Check `ENG_CREW_DASHBOARD_PORT` is not in use: `lsof -i :9000` (macOS/Linux) or `netstat -ano | findstr 9000` (Windows).

**API key errors**
Ensure your `.env` is in the directory where you run `eng-crew`, in
`~/.eng-crew/.env`, or at the root of your checkout — or set the key directly in
your shell environment.

**A ticket is stuck at `in_review` saying "review unavailable"**
The independent reviewer could not run — usually no API key for the provider on
the `ticket_reviewer` role. The ticket's history says which one. Set that key,
switch the role to a provider you do have (`ENG_CREW_TICKET_REVIEWER_PROVIDER`),
or turn the step off with `ENG_CREW_TICKET_REVIEW_ENABLED=false`.

**The Discord bot will not come online**
Run `eng-crew discord` and it prints the reason instead of hiding it in a log:
no `DISCORD_BOT_TOKEN`, the optional extra not installed
(`pip install 'eng-crew[discord]'`), a token Discord rejects, or the Message
Content intent not enabled for your app in the Discord Developer Portal
(your app -> Bot -> Privileged Gateway Intents). Started as a service, the
bot's output is in `<data dir>/logs/discord_bot.log`.

**The project I planned is not in Discord**
Planning files tickets, it does not start a build. `/queue <project>` shows
them and `/build <ticket_id>` builds one; `/projects` lists the aliases.

**Run stuck waiting for approval**
Visit http://localhost:9000, find the pending run, and click Approve — or rerun with `--no-approval`.

**Resume an interrupted run**
```bash
eng-crew resume <run-id>
```
Run IDs are shown in the dashboard and in terminal output.

## Optional MCP integrations

eng-crew works alongside a set of MCP servers that give the agents and your Claude/Gemini CLI sessions persistent memory, smart task prioritisation, and structured reasoning. All three are optional but recommended for the best experience.

---

### yourmemory — persistent memory across sessions

A local, offline-first memory store built in Rust. Any Claude Code or Gemini CLI session running in your project directory shares the same memory — agents remember past decisions, context, and lessons without re-reading files from scratch each time.

**Install:**
```bash
cargo install yourmemory        # installs yourmemory + yourmemory-mcp binaries
cd /your/project
yourmemory init                 # creates .yourmemory/ in the project
yourmemory setup claude         # wires the MCP server into Claude Code
```

Open a new Claude Code session and memory is active. Source: [github.com/VSJ-superhub/rusty-mempalace](https://github.com/VSJ-superhub/rusty-mempalace)

**One canonical palace across all projects (recommended).** `yourmemory setup claude` wires the server automatically, but by default each project's `.yourmemory/` is a separate store — so lessons scatter instead of compounding. To make every project share **one cross-project brain** (the substrate that lets the crew actually get better over time), pin the palace with `YOURMEMORY_PALACE` in `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "yourmemory": {
      "command": "yourmemory-mcp",
      "args": [],
      "env": { "YOURMEMORY_PALACE": "/absolute/path/to/your/.yourmemory" }
    }
  }
}
```
> `YOURMEMORY_PALACE` points at the directory holding `palace.db`. When set, every project's agents read and write the same shared palace regardless of working directory; unset, the server falls back to per-project walk-up resolution.

**Tools exposed:** `wakeup`, `search`, `store_fact`, `recall` (plus `persist`, `update_fact`, `invalidate_fact`, knowledge-graph and maintenance tools).

---

### decisions — task ranking and agent routing

A stateless decisioning engine (Python + Rust core) that ranks task lists, recommends the next highest-value action, and tells you which agent type should handle a given task. Useful when you have a backlog and want the pipeline to work in the right order.

**Install:**
```bash
git clone https://github.com/VSJ-superhub/decisions
cd decisions
maturin develop --release       # builds the Rust scoring core
pip install "mcp[cli]>=1.0"
mcp run src/server.py           # starts the MCP server
```

**Register with Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "decisions": {
      "command": "python",
      "args": ["/path/to/decisions/src/server.py"]
    }
  }
}
```

**Tools exposed:** `score_tasks`, `next_action`, `route_to_agent`, `plan_sprint`

---

### cognitive-stack — structured reasoning layers

A meta-router MCP server that sits in front of 7 cognitive layers (memory, perception, reasoning, decision, planning, action, communication). Call a single `route(task)` tool and it fans out to the right layers in the right order, returning a unified result. Useful for complex architectural decisions or multi-step analysis tasks.

**Install:**
```bash
git clone https://github.com/VSJ-superhub/cognitive-stack
cd cognitive-stack
pip install -e .
python src/server.py            # starts the MCP server
```

**Register with Claude Code** (`~/.claude/settings.json`):
```json
{
  "mcpServers": {
    "cognitive_stack": {
      "command": "python",
      "args": ["/path/to/cognitive-stack/src/server.py"]
    }
  }
}
```

**Tools exposed:** `route(task)`, `status`

---

## License

MIT
