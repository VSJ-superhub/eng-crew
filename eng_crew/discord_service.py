"""Why the Discord bot is — or is not — running.

`discord_bot.py` imports discord.py at module level, so nothing that needs to
*ask about* the bot can import it: the question "why didn't the bot start?"
has to be answerable without the dependency the answer may be about. This
module is that import-safe half — the preflight checks, where the bot's output
goes, and the tail of it.

Before this existed the bot was launched with its output thrown away, so a bot
that died on its first line (no discord.py, a rejected token, an intent the
Discord app has not been granted) was reported as "started" and then, later, as
"stopped", with nothing anywhere saying why.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

TOKEN_MISSING = (
    "DISCORD_BOT_TOKEN is not set — put the bot token from the Discord "
    "Developer Portal (your app -> Bot -> Reset Token) in your .env"
)
LIBRARY_MISSING = (
    "discord.py is not installed — install the optional extra: "
    "pip install 'eng-crew[discord]'"
)
BAD_TOKEN = (
    "Discord rejected DISCORD_BOT_TOKEN — it is wrong, or it was regenerated "
    "and the .env still has the old one. Copy a fresh token from the Developer "
    "Portal (your app -> Bot -> Reset Token)."
)
INTENTS_REFUSED = (
    "Discord refused the bot's Message Content intent, which it needs to read "
    "@mentions and DMs. Enable it in the Developer Portal: your app -> Bot -> "
    "Privileged Gateway Intents -> Message Content, then start the bot again."
)


def log_path() -> Path:
    """Where the bot's stdout/stderr is kept.

    Read through the module rather than importing DATA_DIR once, so a test (or
    a process that re-pointed ENG_CREW_DATA_DIR) sees the directory in force now.
    """
    from . import config

    return Path(config.DATA_DIR) / "logs" / "discord_bot.log"


def open_log():
    """Open the bot's log for appending, creating its directory."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return open(path, "a", encoding="utf-8", errors="replace")


def log_tail(lines: int = 12) -> str:
    """The last non-blank `lines` of the bot's log, or "" if there is none."""
    try:
        text = log_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join([ln for ln in text.splitlines() if ln.strip()][-lines:])


def library_installed() -> bool:
    try:
        return importlib.util.find_spec("discord") is not None
    except (ImportError, ValueError):
        return False


def token_set(env: dict | None = None) -> bool:
    source = os.environ if env is None else env
    return bool((source.get("DISCORD_BOT_TOKEN") or "").strip())


def preflight(env: dict | None = None) -> list[str]:
    """Everything that would stop the bot from starting, in plain language.

    Empty means nothing local is wrong — which is not a promise that Discord
    will accept the token, only that the attempt is worth making.
    """
    problems = []
    if not token_set(env):
        problems.append(TOKEN_MISSING)
    if not library_installed():
        problems.append(LIBRARY_MISSING)
    return problems
