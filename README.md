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
git clone https://github.com/anthropics/eng-crew
cd eng-crew
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and set your LLM provider and API key:

```dotenv
ENG_CREW_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

Supported providers: `anthropic`, `openrouter`, `gemini`

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
| `ENG_CREW_PROVIDER` | `anthropic` | LLM provider: `anthropic`, `openrouter`, `gemini` |
| `ANTHROPIC_API_KEY` | — | Anthropic API key |
| `OPENROUTER_API_KEY` | — | OpenRouter API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
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

## License

MIT
