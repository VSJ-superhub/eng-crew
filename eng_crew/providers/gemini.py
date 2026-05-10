import os
import sys
from typing import Any

from .base import LLMResult, Provider, calculate_cost, QuotaExceeded, ProviderUnavailable


class GeminiProvider(Provider):
    def has_credentials(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def get_client(self) -> Any:
        try:
            from google import genai
        except ImportError:
            raise RuntimeError("google-genai not installed. Run: uv add google-genai")

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ProviderUnavailable("GEMINI_API_KEY not set")

        return genai.Client(api_key=api_key)

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def call(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> LLMResult:
        try:
            from google.genai import types
        except ImportError:
            raise RuntimeError("google-genai not installed. Run: uv add google-genai")

        client = self.get_client()
        
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    max_output_tokens=kwargs.get("max_output_tokens", 8192),
                    temperature=kwargs.get("temperature", 0.2),
                ),
            )
            text = response.text or ""
            meta = response.usage_metadata
            input_tokens = int(getattr(meta, "prompt_token_count", 0) or 0)
            output_tokens = int(getattr(meta, "candidates_token_count", 0) or 0)
            cost = calculate_cost(model, input_tokens, output_tokens)
            return LLMResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost, provider="gemini", model=model)
        except Exception as e:
            print(f"[gemini] API error: {e}", file=sys.stderr)
            raise

def call(model, prompt, **kwargs): return GeminiProvider().call(model, prompt, **kwargs)
