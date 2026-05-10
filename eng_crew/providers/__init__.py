import os
import sys
from typing import Dict, Type

from .base import LLMResult, Provider, calculate_cost, QuotaExceeded, ProviderUnavailable
from .anthropic_api import AnthropicProvider
from .claude_cli import ClaudeCLIProvider
from .gemini import GeminiProvider
from .gemini_cli import GeminiCLIProvider
from .openrouter import OpenRouterProvider
from .deepseek import DeepSeekProvider
from .ollama import OllamaProvider

PROVIDER_REGISTRY: Dict[str, Type[Provider]] = {
    "anthropic":   AnthropicProvider,
    "claude_cli":  ClaudeCLIProvider,
    "gemini":      GeminiProvider,
    "gemini_cli":  GeminiCLIProvider,
    "openrouter":  OpenRouterProvider,
    "deepseek":    DeepSeekProvider,
    "ollama":      OllamaProvider,
}

def get_provider(name: str) -> Provider:
    provider_cls = PROVIDER_REGISTRY.get(name)
    if not provider_cls:
        raise ValueError(f"Unknown provider: {name!r}. Available: {', '.join(PROVIDER_REGISTRY.keys())}")
    return provider_cls()

def call_llm(provider: str, model: str, prompt: str, **kwargs) -> LLMResult:
    try:
        p_instance = get_provider(provider)
        return p_instance.call(model, prompt, **kwargs)
    except Exception as e:
        print(f"[providers] Error calling {provider}/{model}: {e}", file=sys.stderr)
        # Fallback to local Ollama if everything else fails? Or just re-raise
        raise

__all__ = [
    "call_llm", "get_provider", "PROVIDER_REGISTRY",
    "LLMResult", "Provider", "calculate_cost",
    "QuotaExceeded", "ProviderUnavailable"
]
