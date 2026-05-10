"""Named LLM stack presets for the eng-crew pipeline."""
from __future__ import annotations

import os
from typing import Any

STACKS: dict[str, dict[str, Any]] = {
    "quality": {
        "description": "Max quality - Claude Sonnet everywhere",
        "orchestrator": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "architect":    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "coder":        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "reviewer":     {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "executor":     {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"},
    },
    "fast": {
        "description": "Speed-optimised - Gemini Flash everywhere",
        "orchestrator": {"provider": "gemini", "model": "gemini-2.0-flash"},
        "architect":    {"provider": "gemini", "model": "gemini-2.0-flash"},
        "coder":        {"provider": "gemini", "model": "gemini-2.0-flash"},
        "reviewer":     {"provider": "gemini", "model": "gemini-2.0-flash"},
        "executor":     {"provider": "gemini", "model": "gemini-2.0-flash"},
    },
    "local": {
        "description": "Local-first - Ollama coders, Gemini for planning",
        "orchestrator": {"provider": "gemini",  "model": "gemini-2.0-flash"},
        "architect":    {"provider": "gemini",  "model": "gemini-2.0-flash"},
        "coder":        {"provider": "ollama",  "model": "qwen2.5-coder:32b"},
        "reviewer":     {"provider": "gemini",  "model": "gemini-2.0-flash"},
        "executor":     {"provider": "ollama",  "model": "qwen2.5-coder:7b"},
    },
    "deepseek": {
        "description": "DeepSeek R1 reasoning for all tasks",
        "orchestrator": {"provider": "deepseek", "model": "deepseek-chat"},
        "architect":    {"provider": "deepseek", "model": "deepseek-chat"},
        "coder":        {"provider": "deepseek", "model": "deepseek-reasoner"},
        "reviewer":     {"provider": "deepseek", "model": "deepseek-chat"},
        "executor":     {"provider": "deepseek", "model": "deepseek-chat"},
    },
    "free": {
        "description": "Zero API cost - OpenRouter free-tier models",
        "orchestrator": {"provider": "openrouter", "model": "google/gemma-2-9b-it:free"},
        "architect":    {"provider": "openrouter", "model": "google/gemma-2-9b-it:free"},
        "coder":        {"provider": "openrouter", "model": "qwen/qwen3-coder:free"},
        "reviewer":     {"provider": "openrouter", "model": "google/gemma-2-9b-it:free"},
        "executor":     {"provider": "openrouter", "model": "qwen/qwen3-coder:free"},
    },
}


def get_active_stack_name() -> str:
    return os.environ.get("ENG_CREW_STACK", "quality")


def get_stack_config(name: str | None = None) -> dict[str, Any]:
    key = name or get_active_stack_name()
    return STACKS.get(key, STACKS["quality"])
