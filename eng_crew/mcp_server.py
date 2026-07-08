#!/usr/bin/env python3
"""
eng_crew.mcp_server — MCP server exposing eng-crew project tools to Claude.

Launch:  python -m eng_crew.mcp_server

Tools:
  run_task(project_path, task)   — dispatch a task to eng-crew (background, returns run info)
  list_projects()                — registered projects and their paths
  resume_run(run_id)             — resume an interrupted run from its last checkpoint
  start_services(project_path?)  — start dashboard (+ discord bot if token set)
  stop_services(service?)        — stop managed services
  services_status()              — what is running + ports

Design notes:
  - eng-crew defaults to the single-agent tier (one capable agentic CLI call);
    there is no pre-execution plan-approval gate on the default path, so tasks
    are dispatched fire-and-forget with approval disabled. The full multi-agent
    graph (with the dashboard HITL gate) is opt-in via ENG_CREW_ENABLE_MULTI_AGENT.
  - Sprint/plan tooling from the legacy system is intentionally omitted.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ── Paths ───────────────────────────────────────────────────────────────────────
_PKG_DIR = Path(__file__).resolve().parent          # .../eng-crew/eng_crew
_ENG_CREW_ROOT = _PKG_DIR.parent                    # .../eng-crew
_STATE_FILE = _ENG_CREW_ROOT / ".services_state.json"
_VENV_PYTHON = _ENG_CREW_ROOT / ".venv" / "Scripts" / "python.exe"
_PYTHON = str(_VENV_PYTHON) if _VENV_PYTHON.exists() else sys.executable

# ── Environment: load .env so subprocesses inherit credentials ──────────────────
_base_env = dict(os.environ)
_env_path = _ENG_CREW_ROOT / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _base_env.setdefault(_k.strip(), _v.strip())

_DEFAULT_DASH_PORT = int(_base_env.get("ENG_CREW_DASHBOARD_PORT", 9000))

mcp = FastMCP("project-starter")


# ── State helpers ───────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"services": {}}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_free_port(start: int, attempts: int = 10) -> int:
    import socket
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"No free port in range {start}-{start + attempts - 1}")


def _is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != "zombie"
    except ImportError:
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True,
            ).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _popen(args: list[str], cwd: str | None = None, log=None):
    env = {**_base_env, "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen(
        args,
        cwd=cwd or str(_ENG_CREW_ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log or subprocess.DEVNULL,
        stderr=log or subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


# ── Tools ───────────────────────────────────────────────────────────────────────
@mcp.tool()
def run_task(project_path: str, task: str) -> str:
    """
    Dispatch a task to eng-crew for any project.

    Runs the single-agent pipeline as a background process and returns
    immediately with run info. Track progress on the dashboard.

    Args:
        project_path: Absolute path to the project root.
        task:         Natural-language description of what to build or fix.
    """
    proj = Path(project_path).resolve()
    if not proj.is_dir():
        return f"Error: '{project_path}' is not a directory."

    env = {
        **_base_env,
        "PYTHONIOENCODING": "utf-8",
        # Fire-and-forget dispatch: no interactive/HITL gate on the default
        # single-agent path. (The multi-agent gate is opt-in and dashboard-driven.)
        "ENG_CREW_REQUIRE_APPROVAL": "false",
    }

    log_file = _ENG_CREW_ROOT / "logs" / f"run_{int(time.time())}.log"
    log_file.parent.mkdir(exist_ok=True)

    try:
        log = open(log_file, "w")
        proc = subprocess.Popen(
            [_PYTHON, "-u", "-m", "eng_crew", "run", task, str(proj), "--no-approval"],
            cwd=str(_ENG_CREW_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        dash_port = _load_state().get("services", {}).get("dashboard", {}).get("port", _DEFAULT_DASH_PORT)
        return (
            f"Task dispatched (pid={proc.pid})\n"
            f"  Project : {proj}\n"
            f"  Task    : {task}\n"
            f"  Log     : {log_file}\n"
            f"  Track   : http://localhost:{dash_port}\n\n"
            f"Run services_status() or open the dashboard to monitor progress."
        )
    except Exception as e:
        return f"Failed to dispatch task: {e}"


@mcp.tool()
def list_projects() -> str:
    """List all projects registered in the eng-crew database, with their paths."""
    try:
        from eng_crew.tracker import list_projects as _list
        projects = _list(active_only=False)
        if not projects:
            return (
                "No projects registered yet.\n"
                "Projects are registered on first run. Pass the project path "
                "directly to run_task()."
            )
        lines = ["Registered projects:\n"]
        for p in projects:
            lines.append(f"  [{p.get('id', '?')}] {p.get('name', '(unnamed)')}")
            lines.append(f"        path : {p.get('project_path') or p.get('path', '')}")
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing projects: {e}"


@mcp.tool()
def resume_run(run_id: int) -> str:
    """
    Resume an interrupted run from its last checkpoint.

    Args:
        run_id: The run ID shown in the dashboard or returned by run_task().
    """
    env = {**_base_env, "PYTHONIOENCODING": "utf-8", "ENG_CREW_REQUIRE_APPROVAL": "false"}
    log_file = _ENG_CREW_ROOT / "logs" / f"resume_{run_id}_{int(time.time())}.log"
    log_file.parent.mkdir(exist_ok=True)
    try:
        log = open(log_file, "w")
        proc = subprocess.Popen(
            [_PYTHON, "-u", "-m", "eng_crew", "resume", str(run_id)],
            cwd=str(_ENG_CREW_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        return (
            f"Resume dispatched for run #{run_id} (pid={proc.pid})\n"
            f"  Log : {log_file}"
        )
    except Exception as e:
        return f"Failed to resume run #{run_id}: {e}"


@mcp.tool()
def start_services(project_path: str = "") -> str:
    """
    Start the eng-crew service stack:
      - Dashboard    (http://localhost:<ENG_CREW_DASHBOARD_PORT>, default 9000)
      - Discord bot  (only if DISCORD_BOT_TOKEN is set)

    Args:
        project_path: Optional; reserved for future per-project indexing.
    """
    warnings = []
    if not _base_env.get("ANTHROPIC_API_KEY"):
        warnings.append("WARN: ANTHROPIC_API_KEY missing from .env")
    if not (_PKG_DIR / "dashboard" / "static" / "index.html").exists():
        warnings.append("WARN: dashboard frontend not built (cd eng_crew/dashboard/frontend && npm run build)")

    state = _load_state()
    services = state.get("services", {})
    results = list(warnings)
    if warnings:
        results.append("")

    # ── Dashboard ──────────────────────────────────────────────────────────
    dash_pid = services.get("dashboard", {}).get("pid")
    dash_port = services.get("dashboard", {}).get("port", _DEFAULT_DASH_PORT)
    if _port_in_use(dash_port):
        results.append(f"dashboard  already listening -> http://localhost:{dash_port}")
        services["dashboard"] = {"pid": dash_pid or 0, "port": dash_port}
    elif dash_pid and _is_running(dash_pid):
        results.append(f"dashboard  already running (pid {dash_pid}) -> http://localhost:{dash_port}")
    else:
        try:
            dash_port = _find_free_port(_DEFAULT_DASH_PORT)
            env_override = {
                **_base_env,
                "ENG_CREW_DASHBOARD_PORT": str(dash_port),
                "PYTHONIOENCODING": "utf-8",
            }
            proc = subprocess.Popen(
                [_PYTHON, "-m", "eng_crew.dashboard"],
                cwd=str(_ENG_CREW_ROOT),
                env=env_override,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
            )
            services["dashboard"] = {"pid": proc.pid, "port": dash_port}
            results.append(f"dashboard  started (pid {proc.pid}) -> http://localhost:{dash_port}")
            time.sleep(1.5)
        except Exception as e:
            results.append(f"dashboard  FAILED: {e}")

    # ── Discord bot ────────────────────────────────────────────────────────
    discord_pid = services.get("discord", {}).get("pid")
    if discord_pid and _is_running(discord_pid):
        results.append(f"discord    already running (pid {discord_pid})")
    elif not _base_env.get("DISCORD_BOT_TOKEN"):
        results.append("discord    skipped (DISCORD_BOT_TOKEN not set)")
    elif not (_PKG_DIR / "discord_bot.py").exists():
        results.append("discord    skipped (bot not installed)")
    else:
        try:
            proc = _popen([_PYTHON, "-m", "eng_crew.discord_bot"])
            services["discord"] = {"pid": proc.pid}
            results.append(f"discord    started (pid {proc.pid})")
        except Exception as e:
            results.append(f"discord    FAILED: {e}")

    state["services"] = services
    _save_state(state)
    return "Services:\n" + "\n".join(f"  {r}" if r else "" for r in results)


@mcp.tool()
def services_status() -> str:
    """Show the status of managed eng-crew services (dashboard, discord bot)."""
    state = _load_state()
    services = state.get("services", {})
    lines = []

    errors = []
    if not _base_env.get("ANTHROPIC_API_KEY"):
        errors.append("ANTHROPIC_API_KEY missing from .env")
    if not (_PKG_DIR / "dashboard" / "static" / "index.html").exists():
        errors.append("dashboard frontend not built")
    if errors:
        lines.append("Setup issues:")
        lines.extend(f"  - {e}" for e in errors)
        lines.append("")

    if not services:
        lines.append("No services started yet. Call start_services() to begin.")
        return "\n".join(lines)

    lines.append("Services:")
    for name, info in services.items():
        pid = info.get("pid")
        port = info.get("port")
        running = _is_running(pid) if pid else _port_in_use(port) if port else False
        status = "running" if running else "stopped"
        tail = f" -> http://localhost:{port}" if port and running else ""
        lines.append(f"  {name:<10} {status:<8} pid={pid}{tail}")
    return "\n".join(lines)


@mcp.tool()
def stop_services(service: str = "all") -> str:
    """
    Stop managed eng-crew services.

    Args:
        service: 'dashboard', 'discord', or 'all' (default).
    """
    state = _load_state()
    services = state.get("services", {})
    targets = list(services.keys()) if service == "all" else [service]
    stopped, failed = [], []

    for name in targets:
        info = services.get(name)
        if not info:
            failed.append(f"{name}: not tracked")
            continue
        pid = info.get("pid")
        if not pid:
            failed.append(f"{name}: no pid")
            continue
        if not _is_running(pid):
            stopped.append(f"{name} (already stopped)")
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            else:
                os.kill(pid, 15)
            stopped.append(f"{name} (pid={pid})")
        except Exception as e:
            failed.append(f"{name}: {e}")

    if service == "all":
        state["services"] = {}
    else:
        services.pop(service, None)
        state["services"] = services
    _save_state(state)

    out = []
    if stopped:
        out.append("Stopped: " + ", ".join(stopped))
    if failed:
        out.append("Failed:  " + ", ".join(failed))
    return "\n".join(out) or "Nothing to stop."


if __name__ == "__main__":
    mcp.run()
