"""Which MCP servers an agent eng-crew starts is allowed to load.

The danger is twofold: an agent session that inherits the user's own MCP
servers can mail, trade or dispatch on their behalf, and an agent handed
eng-crew's *own* server can dispatch, plan or build other eng-crew work from
inside a run. So the command line is the thing under test here — every CLI call
must carry an explicit MCP configuration plus --strict-mcp-config (which makes
the CLI ignore the global and project servers), and that configuration must
never name eng-crew's own server.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eng_crew import discuss, manager, mcp_policy, review
from eng_crew.agents.single_agent import SingleAgentEngineer
from eng_crew.agents.verifier import VerifierAgent
from eng_crew.config import Settings
from eng_crew.providers.claude_cli import ClaudeCLIProvider

ENG_CREW_SERVER = "project-starter"
A_USER_SERVER = "weather"


# --- capturing real command lines ---------------------------------------


@pytest.fixture
def cmds(monkeypatch):
    """Records the argv of every Claude CLI call made while it is installed."""
    seen: list[list[str]] = []

    def fake_stream(self, cmd, cwd, prompt=""):
        seen.append(list(cmd))
        yield {
            "type": "result",
            "subtype": "success",
            "result": "ok",
            "session_id": "sess",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "total_cost_usd": 0.0,
        }

    monkeypatch.setattr(ClaudeCLIProvider, "_stream", fake_stream)
    return seen


def mcp_config_of(cmd: list[str]) -> dict:
    """The MCP configuration the command line carries. Fails if it carries none."""
    assert mcp_policy.STRICT_FLAG in cmd, (
        f"the call does not pin its MCP servers, so it loads the user's own: {cmd}"
    )
    assert mcp_policy.CONFIG_FLAG in cmd, f"the call has no explicit MCP config: {cmd}"
    payload = cmd[cmd.index(mcp_policy.CONFIG_FLAG) + 1]
    return json.loads(payload)["mcpServers"]


def _failed_verification():
    class _Result:
        def failure_report(self) -> str:
            return "pytest: 1 failed"

    return _Result()


def builder_cmd(cmds) -> list[str]:
    SingleAgentEngineer(Settings()).run(
        {"raw_task": "add a thing", "project_path": ".", "run_id": 0}
    )
    return cmds[-1]


def repair_cmd(cmds) -> list[str]:
    VerifierAgent(Settings())._repair(
        {"raw_task": "add a thing", "project_path": "."},
        _failed_verification(), 0, 1,
    )
    return cmds[-1]


def reviewer_cmd(cmds) -> list[str]:
    ClaudeCLIProvider().call("claude-haiku-4-5-20251001", "judge this",
                             **review.SANDBOX_KWARGS)
    return cmds[-1]


def manager_cmd(cmds) -> list[str]:
    manager.chat("what should we build?", project_path=".")
    return cmds[-1]


def discuss_cmd(cmds) -> list[str]:
    discuss.discuss("how does the queue work?", project_path=".")
    return cmds[-1]


EVERY_ROLE = {
    "builder": builder_cmd,
    "repair": repair_cmd,
    "reviewer": reviewer_cmd,
    "manager": manager_cmd,
    "discussion": discuss_cmd,
}


# --- criterion 1: every CLI call pins its MCP servers --------------------


@pytest.mark.parametrize("role", sorted(EVERY_ROLE))
def test_every_cli_call_runs_with_an_explicit_mcp_configuration(role, cmds):
    cmd = EVERY_ROLE[role](cmds)
    # mcp_config_of asserts both flags; parsing proves the config is real JSON
    # the CLI can read rather than a flag with nothing behind it.
    assert isinstance(mcp_config_of(cmd), dict)


# --- criterion 2: never eng-crew's own server ---------------------------


def test_eng_crews_own_server_is_refused_however_it_is_configured(monkeypatch):
    for spelling in ("project-starter", "eng-crew", "eng_crew", "Project-Starter"):
        spec = json.dumps({spelling: {"command": "x"}, A_USER_SERVER: {"command": "y"}})
        assert mcp_policy.servers(spec) == {A_USER_SERVER: {"command": "y"}}, (
            f"an agent was handed eng-crew's own server as {spelling!r}"
        )


def test_the_servers_name_matches_the_server_eng_crew_actually_runs():
    """If the MCP server is ever renamed, the exclusion must follow it."""
    from eng_crew import mcp_server

    assert mcp_policy.is_own_server(mcp_server.mcp.name)


# --- criterion 3: configurable, empty by default -------------------------


def test_no_extra_servers_by_default():
    assert mcp_policy.servers("") == {}
    assert json.loads(mcp_policy.config_json(""))["mcpServers"] == {}
    assert Settings().agent_mcp_servers == ""


def test_extra_servers_are_configurable_inline_and_by_file(tmp_path):
    inline = json.dumps({"mcpServers": {A_USER_SERVER: {"command": "weather-mcp"}}})
    assert mcp_policy.servers(inline) == {A_USER_SERVER: {"command": "weather-mcp"}}

    path = tmp_path / "agent-mcp.json"
    path.write_text(inline, encoding="utf-8")
    assert mcp_policy.servers(str(path)) == {A_USER_SERVER: {"command": "weather-mcp"}}


def test_the_setting_comes_from_the_environment(monkeypatch):
    spec = json.dumps({A_USER_SERVER: {"command": "weather-mcp"}})
    monkeypatch.setenv("ENG_CREW_AGENT_MCP_SERVERS", spec)
    assert Settings().agent_mcp_servers == spec


def test_the_setting_is_documented_in_env_example():
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "ENG_CREW_AGENT_MCP_SERVERS" in text


def test_unreadable_configuration_means_no_servers_not_the_users_servers(tmp_path):
    assert mcp_policy.servers("{not json") == {}
    assert mcp_policy.servers(str(tmp_path / "nope.json")) == {}


# --- criterion 4: the reviewer stays unable to touch the tree ------------


def test_the_reviewer_still_runs_with_every_file_and_shell_tool_disabled(cmds):
    cmd = reviewer_cmd(cmds)
    denied = cmd[cmd.index("--disallowedTools") + 1]
    for tool in review.NO_TOOLS:
        assert tool in denied, f"the reviewing CLI could still use {tool}"
    assert "--allowedTools" not in cmd
    # Sandboxed means no servers at all, not "the configured ones".
    assert mcp_config_of(cmd) == {}


# --- criterion 5: the three named roles, with a server configured --------


@pytest.mark.parametrize("role", ["builder", "reviewer", "discussion"])
def test_builder_reviewer_and_discussion_exclude_eng_crews_own_server(
    role, cmds, monkeypatch
):
    monkeypatch.setenv(
        "ENG_CREW_AGENT_MCP_SERVERS",
        json.dumps({ENG_CREW_SERVER: {"command": "eng-crew-mcp"},
                    A_USER_SERVER: {"command": "weather-mcp"}}),
    )
    servers = mcp_config_of(EVERY_ROLE[role](cmds))
    assert ENG_CREW_SERVER not in servers, (
        f"the {role} could dispatch, plan or build eng-crew work from inside a run"
    )
    if role != "reviewer":
        assert A_USER_SERVER in servers, "the configured server never reached the agent"
