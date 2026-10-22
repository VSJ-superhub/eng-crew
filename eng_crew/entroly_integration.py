"""Entroly context optimization — optional, off by default.

Entroly is an information-theoretic context optimizer. When it is enabled and
installed, eng-crew uses it in three places:

  - a run refreshes the project's Entroly index before the agents start
  - the project context handed to the agents is compressed to a token budget
  - agents are offered ``entroly`` as an MCP server they can query

Every one of those is best-effort. Entroly is not a dependency of eng-crew and
most installs will not have it, so each entry point answers "no" quietly when
the integration is off or the CLI is missing, and falls back to exactly what
eng-crew did before on any error. Nothing here may fail a run.

Enable with ``ENTROLY_ENABLED=1``; ``ENTROLY_QUALITY`` and
``ENTROLY_TOKEN_BUDGET`` tune it (see .env.example).
Install with ``pip install entroly[native]``.

The settings are read from the environment on each call rather than captured at
import: the dashboard reports them live, and .env is loaded into os.environ
after this module may already have been imported.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

#: Name the MCP server is registered under for the agents.
MCP_SERVER_NAME = "entroly"

DEFAULT_QUALITY = "balanced"
DEFAULT_TOKEN_BUDGET = 8192

_TRUE = ("1", "true", "yes", "on")
_INIT_TIMEOUT = 120


def is_enabled() -> bool:
    """True when ENTROLY_ENABLED asks for the integration. Off by default."""
    return os.environ.get("ENTROLY_ENABLED", "0").strip().lower() in _TRUE


def quality() -> str:
    """The quality profile handed to entroly (ENTROLY_QUALITY)."""
    return os.environ.get("ENTROLY_QUALITY") or DEFAULT_QUALITY


def token_budget() -> int:
    """The token budget the project context is compressed to."""
    raw = os.environ.get("ENTROLY_TOKEN_BUDGET")
    if not raw:
        return DEFAULT_TOKEN_BUDGET
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_TOKEN_BUDGET
    return value if value > 0 else DEFAULT_TOKEN_BUDGET


def entroly_cmd() -> str | None:
    """The entroly CLI as an absolute path, or None when it is not installed.

    ENTROLY_BIN wins — a venv install is not always on the PATH of whatever
    launched eng-crew — and is only believed if the file is actually there.
    """
    override = os.environ.get("ENTROLY_BIN")
    if override:
        path = Path(override).expanduser()
        return str(path) if path.exists() else None
    return shutil.which("entroly")


def is_available() -> bool:
    """True when the entroly CLI is installed and reachable."""
    return entroly_cmd() is not None


def _cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """entroly writes UTF-8; a Windows console codepage mangles it otherwise."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env.update(extra or {})
    return env


def init_project(project_path: str | Path) -> bool:
    """Create or refresh the Entroly index for a project.

    Safe to call on every run — entroly updates an existing index rather than
    rebuilding it. Returns False (never raises) when the integration is off,
    the CLI is missing, or indexing failed: a run is not worth failing over a
    context optimizer.
    """
    if not is_enabled():
        return False
    cmd = entroly_cmd()
    if cmd is None:
        print("[entroly] CLI not found — skipping init "
              "(pip install entroly[native])", file=sys.stderr)
        return False
    try:
        result = subprocess.run(
            [cmd, "init"],
            capture_output=True, text=True, timeout=_INIT_TIMEOUT,
            encoding="utf-8", errors="replace",
            cwd=str(project_path),
            env=_cli_env(),
        )
    except Exception as exc:
        print(f"[entroly] init error: {exc}", file=sys.stderr)
        return False
    if result.returncode == 0:
        print(f"[entroly] index ready for {project_path}", file=sys.stderr)
        return True
    print(f"[entroly] init warning (code {result.returncode}): "
          f"{(result.stderr or '')[:300]}", file=sys.stderr)
    return False


def compress_context(text: str, query: str = "", budget: int = 0) -> str:
    """Compress a project-context string to the token budget.

    Entroly scores the fragments by Shannon entropy and selects an optimal
    subset within the budget. Returns ``text`` unchanged when the integration
    is off, when the entroly package is not installed, and on any error — the
    agents get the uncompressed context rather than none.
    """
    if not is_enabled() or not text.strip():
        return text
    limit = budget or token_budget()
    try:
        from entroly.context_bridge import MultiAgentContext

        ctx = MultiAgentContext(workspace_path=None, token_budget=limit * 4)
        ctx.engine.ingest_fragment(
            content=text, source="project_context", fragment_type="context",
        )
        compressed = ctx.load_hcc_context(
            query=query or "project architecture, what exists, what needs to be built",
            token_budget=limit,
        )
    except ImportError:
        return text  # the CLI can be there without the Python package
    except Exception as exc:
        print(f"[entroly] compress error: {exc}", file=sys.stderr)
        return text
    if not compressed:
        return text
    pct = (1 - len(compressed) / len(text)) * 100 if text else 0.0
    print(f"[entroly] context compressed: {len(text):,} -> {len(compressed):,} "
          f"chars ({pct:.1f}% reduction)", file=sys.stderr)
    return compressed


def mcp_server_entry(project_path: str | Path) -> dict:
    """The mcpServers entry for ``entroly serve`` over one project."""
    return {
        "command": entroly_cmd() or "entroly",
        "args": [],
        "env": {
            "ENTROLY_QUALITY": quality(),
            "ENTROLY_PROJECT_PATH": str(project_path),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
    }


def mcp_servers(project_path: str | Path) -> dict:
    """The MCP servers entroly contributes for a project — {} unless usable."""
    if not is_enabled() or not is_available():
        return {}
    return {MCP_SERVER_NAME: mcp_server_entry(project_path)}


def status() -> dict:
    """What the dashboard card reports: the real, current state."""
    return {
        "enabled": is_enabled(),
        "available": is_available(),
        "quality": quality(),
        "token_budget": token_budget(),
    }
