import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from typing import Any

from .base import LLMResult, Provider, calculate_cost

# Windows CreateProcess / cmd.exe limit is 32767 chars; stay well under it.
_WIN_PROMPT_LIMIT = 6000


class GeminiCLIProvider(Provider):
    def has_credentials(self) -> bool:
        return True

    def get_client(self) -> Any:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def _run_gemini(self, cmd: list, prompt: str, *, cwd: str) -> subprocess.CompletedProcess:
        """Run gemini subprocess.

        On Windows, long prompts exceed the OS command-line length limit when
        passed via -p. For those cases, write the prompt to a temp file and
        pipe it via stdin so the command itself stays short.
        """
        use_shell = platform.system() == "Windows"
        if platform.system() == "Windows" and len(prompt) > _WIN_PROMPT_LIMIT:
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False, encoding="utf-8"
            )
            try:
                tmp.write(prompt)
                tmp.flush()
                tmp.close()
                with open(tmp.name, "r", encoding="utf-8") as stdin_f:
                    return subprocess.run(
                        cmd, stdin=stdin_f,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        cwd=cwd, shell=use_shell,
                    )
            finally:
                try:
                    os.unlink(tmp.name)
                except Exception:
                    pass
        else:
            full_cmd = cmd + ["-p", prompt]
            return subprocess.run(
                full_cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=cwd, shell=use_shell,
            )

    def call(
        self,
        model: str,
        prompt: str,
        **kwargs,
    ) -> LLMResult:
        if not model:
            model = "gemini-2.0-flash"
        cwd = kwargs.get("cwd", ".")

        cmd = ["gemini", "--output-format", "json", "--approval-mode", "yolo", "-m", model]

        try:
            result = self._run_gemini(cmd, prompt, cwd=cwd)
            if result.returncode == 0:
                try:
                    raw_stdout = result.stdout
                    start_idx = raw_stdout.find('{')
                    if start_idx != -1:
                        outer = json.loads(raw_stdout[start_idx:])
                        text = outer.get("response", "")
                        return LLMResult(text=text, provider="gemini_cli", model=model)
                    return LLMResult(text=result.stdout, provider="gemini_cli", model=model)
                except Exception:
                    return LLMResult(text=result.stdout, provider="gemini_cli", model=model)
            else:
                err_detail = (result.stderr or result.stdout or "")[:400].strip()
                print(f"[gemini_cli] Error (code {result.returncode}): {err_detail}", file=sys.stderr)
                raise RuntimeError(f"Gemini CLI error: {err_detail}")
        except RuntimeError:
            raise
        except Exception as e:
            print(f"[gemini_cli] Error: {e}", file=sys.stderr)
            raise

def call(model, prompt, **kwargs): return GeminiCLIProvider().call(model, prompt, **kwargs)
