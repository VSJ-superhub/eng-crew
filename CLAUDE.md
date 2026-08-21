# eng-crew

A public, open-source AI engineering team that autonomously decomposes, codes, reviews, and executes software tasks on any project.

## Goal

This is a generalized, pip-installable version of a private internal tool. It must work out-of-the-box for any developer on any OS with minimal setup.

## Key Requirements

- No hardcoded paths — all config via `.env` or `config.yaml`
- Cross-platform (Windows, macOS, Linux)
- Clean install: `pip install eng-crew` or `docker compose up`
- Multi-agent pipeline: architect → critic → HITL approval → specialist coders → reviewer → executor
- Runs execute in their own git worktree and branch (`worktree_isolation`, on by
  default), so a run never stashes your changes or moves the branch you are on.
- Every execution tier (simple / single-agent / full graph) exits through a deterministic
  verification gate (`eng_crew/verify.py`) that runs the project's own tests and build.
  A run that leaves the tree broken is recorded as `failed`, not `completed`.
- Dashboard at configurable port (default 9000)
- Supports multiple LLM providers: Anthropic Claude CLI, OpenRouter, Gemini
