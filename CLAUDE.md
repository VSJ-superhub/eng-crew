# eng-crew

A public, open-source AI engineering team that autonomously decomposes, codes, reviews, and executes software tasks on any project.

## Goal

This is a generalized, pip-installable version of a private internal tool. It must work out-of-the-box for any developer on any OS with minimal setup.

## Key Requirements

- No hardcoded paths — all config via `.env` or `config.yaml`
- Cross-platform (Windows, macOS, Linux)
- Clean install: `pip install eng-crew` or `docker compose up`
- Multi-agent pipeline: architect → critic → HITL approval → specialist coders → reviewer → executor
- Agent prompts live in `.claude/skills/<name>/SKILL.md` (frontmatter + template),
  loaded by `eng_crew/prompts.py` — edit a prompt without touching agent code.
- Runs execute in their own git worktree and branch (`worktree_isolation`, on by
  default), so a run never stashes your changes or moves the branch you are on.
- Old worktrees are pruned at the start of a run, but only ones that are clean and
  fully merged — an uncommitted worktree is a run's output. `eng-crew worktrees`
  lists them; `eng-crew prune-worktrees --dry-run` shows what would go.
- Every execution tier (simple / single-agent / full graph) exits through a deterministic
  verification gate (`eng_crew/verify.py`) that runs the project's own tests and build.
  A run that leaves the tree broken is recorded as `failed`, not `completed`.
- Dashboard at configurable port (default 9000)
- Supports multiple LLM providers: Anthropic Claude CLI, OpenRouter, Gemini
