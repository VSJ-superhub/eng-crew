"""Claude CLI provider.

Runs the agentic CLI as a subprocess over `--output-format stream-json`, so the
run reports what it is doing while it does it instead of returning one opaque
blob at the end. Callers pass ``on_event`` to observe progress and the returned
LLMResult carries the CLI ``session_id``, which a later call can ``--resume`` to
continue in the same context rather than starting cold.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any, Callable, Iterator

from .base import LLMResult, Provider, calculate_cost

# Prefix applied to output from a run that exhausted its turn budget. The
# verification gate keys off this exact string (see eng_crew.verify).
TRUNCATION_PREFIX = (
    "[TRUNCATED: max turns reached — the implementation may be incomplete]\n"
)

_MAX_RETRIES = 3
_RETRY_DELAYS = [2, 5, 10]


def summarize_event(evt: dict) -> str:
    """One human-readable progress line for a stream event, '' if uninteresting.

    Kept deliberately terse: this text lands in the runs table and the dashboard,
    where a long line is worse than no line.
    """
    etype = evt.get("type")

    if etype == "system" and evt.get("subtype") == "init":
        return f"started ({evt.get('model', 'model?')})"

    if etype == "assistant":
        blocks = (evt.get("message") or {}).get("content") or []
        for block in blocks:
            if block.get("type") == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input") or {}
                target = (
                    inp.get("file_path")
                    or inp.get("pattern")
                    or inp.get("command")
                    or inp.get("description")
                    or ""
                )
                target = str(target).replace("\n", " ")[:60]
                return f"{name}: {target}".strip().rstrip(":")
        for block in blocks:
            if block.get("type") == "text":
                text = (block.get("text") or "").strip().replace("\n", " ")
                if text:
                    return text[:80]
        return ""

    if etype == "result":
        subtype = evt.get("subtype", "")
        turns = evt.get("num_turns", "?")
        if subtype == "error_max_turns":
            return f"stopped: turn limit reached after {turns} turns"
        return f"finished ({turns} turns)"

    return ""


class ClaudeCLIProvider(Provider):
    def has_credentials(self) -> bool:
        return True

    def get_client(self) -> Any:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    # ------------------------------------------------------------------

    def _build_cmd(self, model: str, prompt: str, kwargs: dict) -> list[str]:
        cmd = [
            "claude", "-p", prompt,
            # stream-json requires --verbose when used with --print.
            "--output-format", "stream-json",
            "--verbose",
            "--max-turns", str(kwargs.get("max_turns", 8)),
            "--model", model,
            "--dangerously-skip-permissions",
        ]
        allowed_tools = kwargs.get("allowed_tools")
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])
        resume_session = kwargs.get("resume_session")
        if resume_session:
            cmd.extend(["--resume", str(resume_session)])
        return cmd

    def _stream(self, cmd: list[str], cwd: str) -> Iterator[dict]:
        """Yield parsed events as the CLI emits them."""
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
        )
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON on stdout is not fatal — surface it and continue.
                    print(f"[claude_cli] non-JSON line: {line[:160]}", file=sys.stderr)
        finally:
            if proc.stdout:
                proc.stdout.close()
            stderr = ""
            if proc.stderr:
                stderr = proc.stderr.read()
                proc.stderr.close()
            proc.wait()
            self._last_returncode = proc.returncode
            self._last_stderr = stderr

    def _attempt(self, cmd: list[str], cwd: str, on_event: Callable | None) -> dict:
        """One CLI invocation. Returns a dict of what the stream reported."""
        collected = {
            "text": "",
            "session_id": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "truncated": False,
            "got_result": False,
        }

        for evt in self._stream(cmd, cwd):
            if not collected["session_id"] and evt.get("session_id"):
                collected["session_id"] = evt["session_id"]

            if on_event is not None:
                try:
                    on_event(evt)
                except Exception as exc:  # a bad callback must not kill the run
                    print(f"[claude_cli] on_event error: {exc}", file=sys.stderr)

            if evt.get("type") != "result":
                continue

            collected["got_result"] = True
            collected["text"] = evt.get("result") or ""
            usage = evt.get("usage") or {}
            collected["input_tokens"] = int(usage.get("input_tokens", 0) or 0)
            collected["output_tokens"] = int(usage.get("output_tokens", 0) or 0)
            cost = evt.get("total_cost_usd")
            collected["cost_usd"] = float(cost) if cost is not None else 0.0
            if evt.get("subtype") == "error_max_turns":
                collected["truncated"] = True

        return collected

    def call(self, model: str, prompt: str, **kwargs) -> LLMResult:
        cwd = kwargs.get("cwd", ".")
        on_event = kwargs.get("on_event")
        cmd = self._build_cmd(model, prompt, kwargs)

        text = ""
        session_id = ""
        input_tokens = output_tokens = 0
        cost_usd = 0.0

        for attempt in range(_MAX_RETRIES):
            try:
                got = self._attempt(cmd, cwd, on_event)
            except Exception as exc:
                print(
                    f"[claude_cli] stream error (attempt {attempt + 1}/{_MAX_RETRIES}): {exc}",
                    file=sys.stderr,
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_DELAYS[attempt])
                continue

            session_id = got["session_id"] or session_id
            input_tokens = got["input_tokens"]
            output_tokens = got["output_tokens"]
            cost_usd = got["cost_usd"] or calculate_cost(model, input_tokens, output_tokens)
            text = got["text"]

            if got["truncated"]:
                # Partial work is still useful, but must never read as complete.
                print(
                    f"[claude_cli] max-turns reached — using partial output ({len(text)} chars)",
                    file=sys.stderr,
                )
                text = TRUNCATION_PREFIX + text
                break

            if got["got_result"] and text.strip():
                break

            rc = getattr(self, "_last_returncode", None)
            err = (getattr(self, "_last_stderr", "") or "")[:400].strip()
            print(
                f"[claude_cli] no usable result (rc={rc}): {err} "
                f"(attempt {attempt + 1}/{_MAX_RETRIES})",
                file=sys.stderr,
            )
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAYS[attempt])

        return LLMResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            provider="claude_cli",
            model=model,
            session_id=session_id,
        )


def call(model, prompt, **kwargs):
    return ClaudeCLIProvider().call(model, prompt, **kwargs)
