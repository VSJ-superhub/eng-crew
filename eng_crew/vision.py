"""
Persistent per-project product-vision memory for the Manager.

The Manager grounds ideation in the *code* (Read/Grep). Vision memory is the other
half: your evolving product INTENT — goal, direction, decisions, what's been built,
what's deferred, constraints/preferences — distilled across conversations so the
manager remembers your vision from one session to the next, on any surface.

Stored as a human-readable markdown note per project under DATA_DIR/visions/, so you
can read or hand-edit it. Kept concise and LLM-maintained (merged, not appended).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR
from .providers import call_llm

_VISION_DIR = Path(DATA_DIR) / "visions"
_DISTILL_MODEL = "claude-haiku-4-5-20251001"   # cheap summarization on the subscription


def _file_for(project_path: str) -> Path:
    key = hashlib.sha1(str(project_path).encode("utf-8")).hexdigest()[:16]
    return _VISION_DIR / f"{key}.md"


def get_vision(project_path: str) -> str:
    """The stored vision note for a project, or '' if none yet."""
    try:
        f = _file_for(project_path)
        return f.read_text(encoding="utf-8").strip() if f.exists() else ""
    except Exception as e:
        print(f"[vision] get_vision error: {e}", file=sys.stderr)
        return ""


def set_vision(project_path: str, text: str) -> None:
    try:
        _VISION_DIR.mkdir(parents=True, exist_ok=True)
        _file_for(project_path).write_text((text or "").strip() + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[vision] set_vision error: {e}", file=sys.stderr)


def record_build(project_path: str, task: str) -> None:
    """Cheap, no-LLM: note a dispatched build so it is remembered even before the
    next full distill. The next remember() will merge/clean this into the vision."""
    existing = get_vision(project_path)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    line = f"- [{stamp}] dispatched: {task}"
    if existing:
        set_vision(project_path, existing.rstrip() + "\n" + line)
    else:
        set_vision(project_path, f"# Product Vision\n\n## Build log\n{line}")


_DISTILL_SYSTEM = """\
You maintain a concise, evolving PRODUCT VISION note for a software project. It is read \
by an AI program manager to remember context across conversations.

Given the CURRENT VISION and a RECENT CONVERSATION, produce an UPDATED vision note. Keep \
it tight (under ~300 words). Capture: the product goal and direction, key decisions the \
user made, what has been built or dispatched, what was explicitly deferred, and any \
constraints or preferences the user expressed. MERGE with the current vision — supersede \
stale points, don't just append. Write clear markdown. Output ONLY the updated note."""


def distill(old_vision: str, conversation_text: str, project_name: str = "") -> str:
    """One cheap LLM call: merge a conversation into the existing vision note."""
    prompt = (
        f"{_DISTILL_SYSTEM}\n\n"
        f"=== PROJECT ===\n{project_name or '(unnamed)'}\n\n"
        f"=== CURRENT VISION ===\n{old_vision or '(none yet)'}\n\n"
        f"=== RECENT CONVERSATION ===\n{conversation_text}\n\n"
        f"=== UPDATED VISION ==="
    )
    result = call_llm("claude_cli", _DISTILL_MODEL, prompt, max_turns=1)
    return (result.text or "").strip()


def remember(project_path: str, history: list[dict], project_name: str = "") -> str:
    """Distill an ideation conversation into the stored vision. Blocking (one call).

    `history` is [{"role": "user"|"assistant"|"manager", "content": str}, ...].
    Returns the updated vision text (or the unchanged one if nothing to do).
    """
    if not history:
        return get_vision(project_path)
    convo = "\n".join(
        f"{'USER' if m.get('role') == 'user' else 'MANAGER'}: {m.get('content', '')}"
        for m in history
    )
    updated = distill(get_vision(project_path), convo, project_name)
    if updated:
        set_vision(project_path, updated)
        # Local file is the Manager's fast working memory; also mirror the
        # distilled snapshot into the yourmemory palace as the canonical,
        # cross-project system of record. Best-effort, off the critical path.
        sync_to_palace(project_path, project_name, updated)
        return updated
    return get_vision(project_path)


# ── yourmemory palace sync (best-effort) ────────────────────────────────────────

def _yourmemory_bin() -> str | None:
    """Resolve the yourmemory CLI: env override -> known build -> PATH."""
    cand = os.environ.get("YOURMEMORY_BIN")
    if cand and Path(cand).exists():
        return cand
    known = Path("C:/Users/alway/Projects/yourmemory/target/debug/yourmemory.exe")
    if known.exists():
        return str(known)
    return shutil.which("yourmemory")


def sync_to_palace(project_path: str, project_name: str, vision_text: str) -> None:
    """Mirror the distilled vision into the yourmemory palace as a fact.

    Best-effort: never raises, short timeout. The local vision file remains the
    source the Manager reads on every turn; the palace is the durable,
    cross-project record.
    """
    if not (vision_text or "").strip():
        return
    binp = _yourmemory_bin()
    if not binp:
        return
    header = f"[eng-crew product vision — {project_name or project_path}]"
    fact = f"{header}\n{vision_text.strip()}"
    try:
        subprocess.run(
            [binp, "persist", fact],
            capture_output=True,
            timeout=20,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except Exception as e:
        print(f"[vision] palace sync skipped: {e}", file=sys.stderr)
