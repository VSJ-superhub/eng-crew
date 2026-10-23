"""What MCP servers an agent eng-crew starts is allowed to load.

An agent CLI session inherits the user's own MCP servers unless it is told not
to, which means a build could mail, trade or dispatch on their behalf. Worse,
one of those servers is usually eng-crew itself: a builder that can call
``run_task`` can dispatch, plan or build other eng-crew work from inside a run.

So every CLI call eng-crew makes is launched with an explicit MCP configuration
and ``--strict-mcp-config``, which makes the CLI ignore the user's global and
project servers entirely. What an agent *does* get is configured here
(``ENG_CREW_AGENT_MCP_SERVERS``), empty by default, and eng-crew's own server is
removed from it whatever the configuration says.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

#: The environment variable / setting naming the servers agents may load.
SETTING_ENV = "ENG_CREW_AGENT_MCP_SERVERS"

#: The CLI flags. ``--strict-mcp-config`` is what discards the user's global
#: and project ``.mcp.json``; without it an explicit config is merely additive.
CONFIG_FLAG = "--mcp-config"
STRICT_FLAG = "--strict-mcp-config"

#: eng-crew's own MCP server, under every spelling it gets registered as (see
#: eng_crew.mcp_server, which names it "project-starter"). Never given to an
#: agent — a run must not be able to start other runs.
OWN_SERVER_NAMES = ("project-starter", "eng-crew", "eng-crew-mcp")

_UNSET = object()


def _key(name: Any) -> str:
    """A server name reduced to what distinguishes it: case and the
    dash/underscore spelling are not a different server."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


_OWN_KEYS = frozenset(_key(n) for n in OWN_SERVER_NAMES)


def is_own_server(name: Any) -> bool:
    """Is this eng-crew's own MCP server, however it is spelled?"""
    return _key(name) in _OWN_KEYS


def _configured_spec() -> str:
    """What the environment or config says agents may load.

    The environment is read live rather than through the settings singleton, so
    changing it takes effect for the next run without a restart.
    """
    env = os.environ.get(SETTING_ENV)
    if env is not None:
        return env
    from .config import settings

    return str(getattr(settings, "agent_mcp_servers", "") or "")


def _parse(raw: str) -> dict:
    """The spec as a mapping of server name -> definition.

    Accepts inline JSON or a path to a JSON file, in either the
    ``{"mcpServers": {...}}`` shape the CLI writes or a bare mapping. A spec
    that cannot be read yields nothing: failing closed leaves the agent with no
    servers, which is the safe end of the mistake.
    """
    text = raw.strip()
    if not text.startswith("{"):
        try:
            text = Path(os.path.expanduser(text)).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[mcp_policy] cannot read {SETTING_ENV} file: {exc}", file=sys.stderr)
            return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[mcp_policy] {SETTING_ENV} is not valid JSON: {exc}", file=sys.stderr)
        return {}
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    return data if isinstance(data, dict) else {}


def servers(spec: Any = _UNSET) -> dict:
    """The MCP servers an agent may load, eng-crew's own always removed."""
    raw = str(_configured_spec() if spec is _UNSET else (spec or ""))
    if not raw.strip():
        return {}
    allowed = {}
    for name, definition in _parse(raw).items():
        if is_own_server(name):
            print(
                f"[mcp_policy] refusing to hand an agent eng-crew's own MCP "
                f"server ({name}) — a run may not start other runs",
                file=sys.stderr,
            )
            continue
        allowed[str(name)] = definition
    return allowed


def config_json(
    spec: Any = _UNSET, *, allow_servers: bool = True, extra: dict | None = None
) -> str:
    """The ``--mcp-config`` payload. ``allow_servers=False`` means none at all —
    what a sandboxed session (the ticket reviewer) gets. ``extra`` is servers
    eng-crew's own integrations add (entroly); the own-server rule still holds."""
    if not allow_servers:
        return json.dumps({"mcpServers": {}})
    merged = servers(spec)
    for name, definition in (extra or {}).items():
        if not is_own_server(name):
            merged[str(name)] = definition
    return json.dumps({"mcpServers": merged})


def cli_args(
    spec: Any = _UNSET, *, allow_servers: bool = True, extra: dict | None = None
) -> list[str]:
    """The flags every agent CLI call carries."""
    return [
        CONFIG_FLAG,
        config_json(spec, allow_servers=allow_servers, extra=extra),
        STRICT_FLAG,
    ]
