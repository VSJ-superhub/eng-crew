"""A Discord bot that fails to start has to say why.

The bot used to be launched with its output sent to the void: a crash on its
first line (no discord.py, a rejected token) was reported as "started", then as
"stopped", and nothing anywhere recorded the reason. These tests pin the
diagnosis path — preflight, the log, and what start_services/services_status
report when the process dies.
"""
from __future__ import annotations

import pytest

from eng_crew import discord_service


def _point_data_dir(monkeypatch, tmp_path):
    from eng_crew import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def test_preflight_is_quiet_when_token_and_library_are_present(monkeypatch):
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    assert discord_service.preflight({"DISCORD_BOT_TOKEN": "abc"}) == []


def test_preflight_names_a_missing_token(monkeypatch):
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    problems = discord_service.preflight({})
    assert problems == [discord_service.TOKEN_MISSING]
    assert "DISCORD_BOT_TOKEN" in problems[0]


def test_preflight_ignores_a_blank_token(monkeypatch):
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    assert discord_service.preflight({"DISCORD_BOT_TOKEN": "   "}) == [
        discord_service.TOKEN_MISSING
    ]


def test_preflight_names_the_missing_extra(monkeypatch):
    monkeypatch.setattr(discord_service, "library_installed", lambda: False)
    problems = discord_service.preflight({"DISCORD_BOT_TOKEN": "abc"})
    assert problems == [discord_service.LIBRARY_MISSING]
    assert "eng-crew[discord]" in problems[0]


def test_log_path_follows_the_data_dir(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    assert discord_service.log_path() == tmp_path / "logs" / "discord_bot.log"


def test_log_tail_is_empty_when_the_bot_never_ran(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    assert discord_service.log_tail() == ""


def test_log_tail_returns_the_last_lines(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    with discord_service.open_log() as log:
        log.write("\n".join(f"line {i}" for i in range(20)) + "\n")
    tail = discord_service.log_tail(3).splitlines()
    assert tail == ["line 17", "line 18", "line 19"]


def test_open_log_appends_rather_than_truncating(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    with discord_service.open_log() as log:
        log.write("first run\n")
    with discord_service.open_log() as log:
        log.write("second run\n")
    assert discord_service.log_tail() == "first run\nsecond run"


# ── start_services / services_status ────────────────────────────────────────────

mcp_server = pytest.importorskip(
    "eng_crew.mcp_server", reason="mcp SDK not installed (optional [mcp] extra)"
)


class _Proc:
    def __init__(self, pid=4321):
        self.pid = pid


@pytest.fixture
def quiet_dashboard(monkeypatch):
    """Make start_services' dashboard half a no-op so only Discord is in view."""
    monkeypatch.setattr(mcp_server, "_port_in_use", lambda port: True)
    monkeypatch.setattr(mcp_server, "_save_state", lambda state: None)
    monkeypatch.setattr(mcp_server, "_load_state", lambda: {"services": {}})
    monkeypatch.setattr(mcp_server, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))


def test_start_services_explains_a_skipped_bot(monkeypatch, quiet_dashboard):
    monkeypatch.setattr(mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k"})
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    out = mcp_server.start_services()
    assert "discord    skipped" in out
    assert "DISCORD_BOT_TOKEN" in out


def test_start_services_reports_a_bot_that_died_with_its_log(
    monkeypatch, tmp_path, quiet_dashboard
):
    _point_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k", "DISCORD_BOT_TOKEN": "abc"}
    )
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    monkeypatch.setattr(mcp_server, "_is_running", lambda pid: False)

    def fake_popen(args, cwd=None, log=None):
        assert log is not None, "the bot's output must go to a log, not DEVNULL"
        log.write("ModuleNotFoundError: No module named 'discord'\n")
        log.flush()
        return _Proc()

    monkeypatch.setattr(mcp_server, "_popen", fake_popen)

    out = mcp_server.start_services()
    assert "discord    FAILED" in out
    assert "No module named 'discord'" in out
    assert str(discord_service.log_path()) in out


def test_start_services_records_a_bot_that_stayed_up(monkeypatch, tmp_path, quiet_dashboard):
    _point_data_dir(monkeypatch, tmp_path)
    saved: dict = {}
    monkeypatch.setattr(mcp_server, "_save_state", lambda state: saved.update(state))
    monkeypatch.setattr(
        mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k", "DISCORD_BOT_TOKEN": "abc"}
    )
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)
    monkeypatch.setattr(mcp_server, "_is_running", lambda pid: True)
    monkeypatch.setattr(mcp_server, "_popen", lambda args, cwd=None, log=None: _Proc())

    out = mcp_server.start_services()
    assert "discord    started (pid 4321)" in out
    assert saved["services"]["discord"]["pid"] == 4321


def test_services_status_shows_why_a_stopped_bot_is_stopped(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    with discord_service.open_log() as log:
        log.write("SystemExit: " + discord_service.BAD_TOKEN + "\n")
    monkeypatch.setattr(
        mcp_server, "_load_state", lambda: {"services": {"discord": {"pid": 4321}}}
    )
    monkeypatch.setattr(mcp_server, "_is_running", lambda pid: False)
    monkeypatch.setattr(
        mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k", "DISCORD_BOT_TOKEN": "abc"}
    )
    monkeypatch.setattr(discord_service, "library_installed", lambda: True)

    out = mcp_server.services_status()
    assert "discord    stopped" in out
    assert "Reset Token" in out


def test_services_status_flags_the_missing_extra_once_a_token_is_set(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_server, "_load_state", lambda: {"services": {}})
    monkeypatch.setattr(
        mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k", "DISCORD_BOT_TOKEN": "abc"}
    )
    monkeypatch.setattr(discord_service, "library_installed", lambda: False)
    assert discord_service.LIBRARY_MISSING in mcp_server.services_status()


def test_services_status_does_not_nag_without_a_token(monkeypatch, tmp_path):
    _point_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(mcp_server, "_load_state", lambda: {"services": {}})
    monkeypatch.setattr(mcp_server, "_base_env", {"ANTHROPIC_API_KEY": "k"})
    monkeypatch.setattr(discord_service, "library_installed", lambda: False)
    assert discord_service.LIBRARY_MISSING not in mcp_server.services_status()
