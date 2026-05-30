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

        _MAX_RETRIES = 3
        _RETRY_DELAYS = [2, 5, 10]

        text = ""
        input_tokens = 0
        output_tokens = 0
        cost_usd = 0.0

        for attempt in range(_MAX_RETRIES):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                    cwd=cwd
                )
            except Exception as e:
                print(f"[claude_cli] Subprocess error (attempt {attempt + 1}/{_MAX_RETRIES}): {e}", file=sys.stderr)
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                continue

            if result.returncode == 0:
                try:
                    outer = json.loads(result.stdout)
                    text = outer.get("result", outer.get("content", "")) or ""
                    usage = outer.get("usage") or {}
                    input_tokens = int(usage.get("input_tokens", 0) or 0)
                    output_tokens = int(usage.get("output_tokens", 0) or 0)
                    cost_usd = float(usage.get("cost_usd") or calculate_cost(model, input_tokens, output_tokens))
                except Exception:
                    text = result.stdout
                if text.strip():
                    break
                print(f"[claude_cli] Empty output (attempt {attempt + 1}/{_MAX_RETRIES})", file=sys.stderr)
            else:
                # Fall back to stdout if stderr is empty
                err_detail = (result.stderr or result.stdout or "")[:400].strip()
                print(f"[claude_cli] Error (code {result.returncode}): {err_detail} (attempt {attempt + 1}/{_MAX_RETRIES})", file=sys.stderr)

            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])

        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            provider="claude_cli",
            model=model,
        )

def call(model, prompt, **kwargs): return ClaudeCLIProvider().call(model, prompt, **kwargs)
