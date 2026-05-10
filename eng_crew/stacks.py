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


AVAILABLE_MODELS: dict[str, list[str]] = {
    "anthropic":   ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "gemini":      ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-pro"],
    "openrouter":  ["openai/gpt-4o", "anthropic/claude-3.5-sonnet", "qwen/qwen3-coder:free"],
    "deepseek":    ["deepseek-chat", "deepseek-reasoner"],
    "ollama":      ["qwen2.5-coder:32b", "qwen2.5-coder:7b", "llama3.2"],
    "claude_cli":  ["claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
    "gemini_cli":  ["gemini-2.5-flash", "gemini-2.0-flash"],
}

_active_stack: str = os.environ.get("ENG_CREW_STACK", "quality")
_custom_overrides: dict[str, Any] = {}


def get_active_stack_name() -> str:
    return _active_stack


def get_active_stack() -> str:
    return _active_stack


def get_stack_config(name: str | None = None) -> dict[str, Any]:
    key = name or _active_stack
    return STACKS.get(key, STACKS["quality"])


def get_custom_overrides() -> dict[str, Any]:
    return dict(_custom_overrides)


def set_custom_overrides(overrides: dict[str, Any]) -> None:
    global _custom_overrides
    _custom_overrides = dict(overrides)


def set_active_stack(name: str) -> None:
    global _active_stack
    if name in STACKS:
        _active_stack = name
        os.environ["ENG_CREW_STACK"] = name


def apply_stack(name: str) -> None:
    set_active_stack(name)
