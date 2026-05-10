import json
import os
import subprocess
import sys
import time
from typing import Any

from .base import LLMResult, Provider, calculate_cost


class GeminiCLIProvider(Provider):
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
        if not model: model = "gemini-2.0-flash"
        cwd = kwargs.get("cwd", ".")
        
        # Use shell=True on Windows because gemini is often a .ps1 or .cmd
        import platform
        use_shell = platform.system() == "Windows"
        
        cmd = ["gemini", "-p", prompt, "--output-format", "json", "--approval-mode", "yolo", "-m", model]
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=cwd, shell=use_shell
            )
            if result.returncode == 0:
                try:
                    raw_stdout = result.stdout
                    start_idx = raw_stdout.find('{')
                    if start_idx != -1:
                        outer = json.loads(raw_stdout[start_idx:])
                        text = outer.get("response", "")
                        return LLMResult(text=text, provider="gemini_cli", model=model)
                    return LLMResult(text=result.stdout, provider="gemini_cli", model=model)
                except:
                    return LLMResult(text=result.stdout, provider="gemini_cli", model=model)
            else:
                raise RuntimeError(f"Gemini CLI error: {result.stderr}")
        except Exception as e:
            print(f"[gemini_cli] Error: {e}", file=sys.stderr)
            raise

def call(model, prompt, **kwargs): return GeminiCLIProvider().call(model, prompt, **kwargs)
