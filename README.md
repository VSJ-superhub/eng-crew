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
| `ENG_CREW_CODER_PARALLELISM` | `2` | Number of parallel coder agents (1–8) |
| `ENG_CREW_DATA_DIR` | `.eng-crew` | Directory for run artifacts and checkpoints |

## CLI reference

```
eng-crew run      --project PATH  "task description"   Run a task
eng-crew resume   RUN_ID                               Resume an interrupted run
eng-crew status                                         Show recent runs
eng-crew dashboard                                      Open the dashboard
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'eng_crew'`**
Run `pip install -e .` from the project root, or `pip install eng-crew` from PyPI.

**Dashboard not opening**
Check `ENG_CREW_DASHBOARD_PORT` is not in use: `lsof -i :9000` (macOS/Linux) or `netstat -ano | findstr 9000` (Windows).

**API key errors**
Ensure your `.env` file is in the directory where you run `eng-crew`, or set the key directly in your shell environment.

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
