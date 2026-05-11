import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

from .base import LLMResult, Provider, calculate_cost


class ClaudeCLIProvider(Provider):
    def has_credentials(self) -> bool:
        return True

    def get_client(self) -> Any:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def call(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> LLMResult:
        max_turns = kwargs.get("max_turns", 8)
        cwd = kwargs.get("cwd", ".")
        
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--model", model,
            "--dangerously-skip-permissions",
        ]
        allowed_tools = kwargs.get("allowed_tools")
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=cwd
            )
            if result.returncode == 0:
                try:
                    outer = json.loads(result.stdout)
                    text = outer.get("result", outer.get("content", "")) or result.stdout
                    usage = outer.get("usage") or {}
                    input_tokens = int(usage.get("input_tokens", 0) or 0)
                    output_tokens = int(usage.get("output_tokens", 0) or 0)
                    cost = float(usage.get("cost_usd") or calculate_cost(model, input_tokens, output_tokens))
                    return LLMResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost, provider="claude_cli", model=model)
                except:
                    return LLMResult(text=result.stdout, provider="claude_cli", model=model)
            else:
                raise RuntimeError(f"Claude CLI error: {result.stderr}")
        except Exception as e:
            print(f"[claude_cli] Error: {e}", file=sys.stderr)
            raise

def call(model, prompt, **kwargs): return ClaudeCLIProvider().call(model, prompt, **kwargs)
