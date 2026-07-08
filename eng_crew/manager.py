"""
Grounded Manager (PM) agent — ideates against the real codebase, then dispatches builds.

Surface-agnostic: the dashboard, Discord, and a CLI are thin frontends. The manager
holds a conversation (history is passed in per turn), explores the *actual* code with
read-only tools while it ideates, and — when a concrete, buildable unit of work has
emerged and the user wants to move forward — proposes it. On the human's confirm,
`dispatch()` hands the task to the single-agent builder (`run.run_task`) on a
background thread and returns a run_id to poll.

Design invariants:
  - The manager is GROUNDED: claude_cli with Read/Grep/Glob and cwd=project, so its
    ideas reflect the real code, not a vacuum. (Contrast the old dashboard intake,
    which ran with --allowedTools none --max-turns 1.)
  - The manager NEVER writes code. It has read-only tools; building is a separate,
    explicit, human-confirmed step.
  - Ideation runs on the Claude subscription (claude_cli) — effectively flat cost.
    Metered spend happens only when a build is dispatched.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Optional

from .providers import call_llm

# Grounded ideation: strong reasoning, read-only tools, room to explore a few files.
MANAGER_PROVIDER = "claude_cli"
MANAGER_MODEL = "claude-sonnet-4-6"
MANAGER_TOOLS = "Read,Grep,Glob"
MANAGER_MAX_TURNS = 10

_SYSTEM = """\
You are a senior engineering program manager and tech lead. A product person (the \
"program manager") talks to you in natural language about what they want. Your job is \
to IDEATE WITH THEM and turn fuzzy intent into one concrete, buildable unit of work — \
grounded in the real codebase.

How you work:
- Ground every idea in reality. Use Read, Grep, and Glob to inspect the actual project \
before you propose anything. Reference what you find ("you already have X in file Y, so \
this would hook in there").
- Ask at most 1-2 focused clarifying questions at a time. Do not interrogate.
- Be concise. This is a fast planning conversation on a phone or a chat panel, not a doc.
- You do NOT write code. When work is ready, it is dispatched to the engineering team, \
which implements it on an isolated branch.
- Propose ONE buildable unit at a time — the smallest coherent slice that delivers value.

When (and only when) you and the user have converged on something concrete AND the user \
signals they want to move forward, end your reply with EXACTLY one fenced block:

```build
{"task": "<one precise, self-contained task the engineering team can act on without \
further questions>", "rationale": "<one sentence: why this, and how it fits the code>"}
```

Rules for the build block:
- Emit it only when the user is ready to build — not while still exploring options.
- The "task" must be specific and intent-level (name the outcome), but must NOT invent \
file paths or APIs you did not verify by reading the code.
- Never emit more than one build block. If you are still ideating, do not emit one at all.\
"""

_BUILD_RE = re.compile(r"```build\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass
class ManagerReply:
    """One manager turn. `proposal` is set only when a build is ready to confirm."""
    reply: str
    proposal: Optional[dict] = None   # {"task": str, "rationale": str}


def _extract_proposal(text: str) -> tuple[str, Optional[dict]]:
    """Split a manager response into (conversational reply, optional build proposal)."""
    m = _BUILD_RE.search(text or "")
    if not m:
        return (text or "").strip(), None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return (text or "").strip(), None
    reply = ((text[: m.start()] + text[m.end():]) or "").strip()
    task = str(obj.get("task", "")).strip()
    if not task:
        return reply, None
    return reply, {"task": task, "rationale": str(obj.get("rationale", "")).strip()}


def _build_prompt(history: list[dict], message: str, project_context: str) -> str:
    parts = [_SYSTEM]
    if project_context:
        parts.append(f"\n=== PROJECT ORIENTATION (for context; verify against the code) ===\n{project_context}")
    if history:
        parts.append("\n=== CONVERSATION SO FAR ===")
        for m in history:
            role = "USER" if m.get("role") == "user" else "MANAGER"
            parts.append(f"{role}: {m.get('content', '')}")
    parts.append(f"\n=== NEW USER MESSAGE ===\n{message}")
    parts.append("\nRespond concisely as the program manager. Explore the code with your tools when it helps ground the idea.")
    return "\n".join(parts)


def chat(
    message: str,
    history: Optional[list[dict]] = None,
    project_path: str = ".",
    project_context: str = "",
) -> ManagerReply:
    """One grounded manager turn.

    Args:
        message:         the user's new message.
        history:         prior turns as [{"role": "user"|"assistant"|"manager", "content": str}].
        project_path:    repo root — becomes claude_cli's cwd so Read/Grep/Glob hit real files.
        project_context: optional orientation text (rendered project summary).

    Returns a ManagerReply(reply, proposal).
    """
    prompt = _build_prompt(history or [], message, project_context)
    result = call_llm(
        MANAGER_PROVIDER,
        MANAGER_MODEL,
        prompt,
        allowed_tools=MANAGER_TOOLS,
        max_turns=MANAGER_MAX_TURNS,
        cwd=project_path,
    )
    reply, proposal = _extract_proposal(result.text or "")
    return ManagerReply(reply=reply or "(no response)", proposal=proposal)


def dispatch(task: str, project_path: str, *, claude_md_path: str = "") -> int:
    """Dispatch a confirmed task to the single-agent builder on a background thread.

    Returns a run_id immediately; the build runs asynchronously. Poll status(run_id)
    (or the dashboard) for progress.
    """
    from . import tracker
    from .config import load_settings
    from .run import run_task

    # Single-agent default has no HITL gate; disable approval so the build proceeds.
    settings = load_settings().model_copy(update={"require_approval": False})
    run_id = tracker.create_run(task, project_path)

    def _bg() -> None:
        try:
            run_task(task=task, project_path=project_path, run_id=run_id, settings=settings)
        except Exception as e:  # run_pipeline finishes the run on its own errors; this is a backstop
            try:
                tracker.finish_run(run_id, status="failed", final_summary=str(e))
            except Exception:
                pass

    threading.Thread(target=_bg, daemon=True).start()
    return run_id


def status(run_id: int) -> Optional[dict]:
    """Current status/detail for a dispatched build."""
    from . import tracker
    return tracker.get_run_detail(run_id)
