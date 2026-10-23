"""Entroly is an optional context optimizer, so the only thing it must never do
is matter when it is off.

These cover the three states that exist in the field — off, on but not
installed, on and installed — plus the two places a failure would otherwise
reach the user: a failed index must not fail the run, and a failed compression
must hand the agent the uncompressed context rather than nothing. The CLI is
stubbed; nothing here runs entroly for real.
"""
from __future__ import annotations

import json
import pathlib
import sys
import types

import pytest

from eng_crew import entroly_integration as entroly

ENV_VARS = ("ENTROLY_ENABLED", "ENTROLY_QUALITY", "ENTROLY_TOKEN_BUDGET", "ENTROLY_BIN")
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """A developer's own .env must not decide what these tests see."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_subprocess(monkeypatch):
    """Record any attempt to run the CLI; running one for real is a failure."""
    calls: list[dict] = []

    def record(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(entroly.subprocess, "run", record)
    return calls


@pytest.fixture
def installed(monkeypatch, tmp_path, no_subprocess):
    """Entroly on, with the CLI stubbed: a resolvable binary, no real calls."""
    stub = tmp_path / "entroly-stub"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("ENTROLY_ENABLED", "1")
    monkeypatch.setenv("ENTROLY_BIN", str(stub))
    return no_subprocess


@pytest.fixture
def fake_entroly_package(monkeypatch):
    """Stand in for the `entroly` Python package `compress_context` imports.

    Returns a dict the test fills in: ``result`` is what the compressor
    returns, ``error`` an exception it raises instead.
    """
    behaviour: dict = {"result": "", "error": None, "seen": {}}

    class FakeEngine:
        def ingest_fragment(self, content, source="", fragment_type=""):
            behaviour["seen"]["content"] = content

    class MultiAgentContext:
        def __init__(self, workspace_path=None, token_budget=0):
            behaviour["seen"]["ctor_budget"] = token_budget
            self.engine = FakeEngine()

        def load_hcc_context(self, query="", token_budget=0):
            behaviour["seen"]["query"] = query
            behaviour["seen"]["budget"] = token_budget
            if behaviour["error"] is not None:
                raise behaviour["error"]
            return behaviour["result"]

    pkg = types.ModuleType("entroly")
    bridge = types.ModuleType("entroly.context_bridge")
    bridge.MultiAgentContext = MultiAgentContext
    pkg.context_bridge = bridge
    monkeypatch.setitem(sys.modules, "entroly", pkg)
    monkeypatch.setitem(sys.modules, "entroly.context_bridge", bridge)
    return behaviour


# --- criterion 1: the three settings, and their documentation --------------


def test_the_three_settings_default_off_and_are_read_from_the_env(monkeypatch):
    assert entroly.is_enabled() is False, "Entroly must be off unless asked for"
    assert entroly.quality() == entroly.DEFAULT_QUALITY
    assert entroly.token_budget() == entroly.DEFAULT_TOKEN_BUDGET

    monkeypatch.setenv("ENTROLY_ENABLED", "1")
    monkeypatch.setenv("ENTROLY_QUALITY", "max")
    monkeypatch.setenv("ENTROLY_TOKEN_BUDGET", "1234")
    assert entroly.is_enabled() is True
    assert entroly.quality() == "max"
    assert entroly.token_budget() == 1234

    # And the honoured values are the ones handed on, not the defaults.
    monkeypatch.setenv("ENTROLY_BIN", __file__)  # any existing file resolves
    entry = entroly.mcp_server_entry("/some/project")
    assert entry["env"]["ENTROLY_QUALITY"] == "max"


def test_the_three_settings_are_documented_in_env_example():
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("ENTROLY_ENABLED", "ENTROLY_QUALITY", "ENTROLY_TOKEN_BUDGET"):
        assert name in text, f"{name} is not documented in .env.example"


# --- criterion 2: off, or not installed, changes nothing ------------------


def test_off_is_inert(no_subprocess):
    context = "# CLAUDE.md\nthe whole project context\n"
    assert entroly.is_enabled() is False
    assert entroly.init_project("/some/project") is False
    assert entroly.compress_context(context, query="anything") == context
    assert entroly.mcp_servers("/some/project") == {}
    assert no_subprocess == [], "a disabled integration must not run the CLI"


def test_entroly_is_an_optional_dependency():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    deps = pyproject.split("[project.optional-dependencies]")[0]
    assert "entroly" not in deps, "entroly must not be a required dependency"
    # The integration module itself imports with no entroly package present.
    assert entroly.compress_context("x") == "x"


def test_on_but_not_installed_does_nothing_and_never_raises(monkeypatch, no_subprocess):
    monkeypatch.setenv("ENTROLY_ENABLED", "1")
    monkeypatch.setenv("ENTROLY_BIN", "/nowhere/entroly-does-not-exist")
    monkeypatch.setattr(entroly.shutil, "which", lambda name: None)

    context = "project context"
    assert entroly.is_available() is False
    assert entroly.init_project("/some/project") is False
    assert entroly.mcp_servers("/some/project") == {}
    assert entroly.compress_context(context) == context
    assert no_subprocess == [], "a missing CLI must not be invoked"

    status = entroly.status()
    assert status["enabled"] is True and status["available"] is False


# --- criterion 3: a run indexes the project, and survives a failure -------


def test_init_project_refreshes_the_index_in_the_project_directory(installed):
    assert entroly.init_project("/some/project") is True
    assert len(installed) == 1, installed
    call = installed[0]
    assert call["cmd"][1:] == ["init"]
    assert call["cwd"] == "/some/project"


def test_a_run_indexes_the_project_and_an_indexing_failure_does_not_fail_it(
    tmp_path, monkeypatch
):
    from eng_crew import pipeline, tracker
    from eng_crew.config import Settings

    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs.db")
    tracker.close_connection()
    tracker._init_db()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# a project\n", encoding="utf-8")

    indexed: list[str] = []

    def boom(path):
        indexed.append(path)
        raise RuntimeError("entroly index blew up")

    monkeypatch.setattr(pipeline.entroly_integration, "init_project", boom)

    class FakeGraph:
        def invoke(self, state):
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: FakeGraph())
    settings = Settings()
    settings.worktree_isolation = False
    settings.commit_run_output = False
    try:
        state = pipeline.run_pipeline(
            task="do a thing", project_path=str(project), settings=settings
        )
    finally:
        tracker.close_connection()

    assert indexed, "the run never asked entroly to refresh the index"
    assert state.get("final_summary") == "done", "an indexing failure failed the run"


# --- criterion 4: the context handed to agents is compressed --------------


def test_the_context_a_run_hands_agents_is_compressed_to_the_budget(
    tmp_path, monkeypatch, installed, fake_entroly_package
):
    from eng_crew import pipeline, tracker
    from eng_crew.config import Settings

    monkeypatch.setenv("ENTROLY_TOKEN_BUDGET", "777")
    fake_entroly_package["result"] = "COMPRESSED"

    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "runs2.db")
    tracker.close_connection()
    tracker._init_db()
    project = tmp_path / "proj2"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# long original context\n" * 50, encoding="utf-8")

    captured: dict = {}

    class FakeGraph:
        def invoke(self, state):
            captured.update(state)
            return {**state, "final_summary": "done", "verification_passed": True}

    monkeypatch.setattr(pipeline, "_build_graph", lambda s: FakeGraph())
    settings = Settings()
    settings.worktree_isolation = False
    settings.commit_run_output = False
    try:
        pipeline.run_pipeline(
            task="do a thing", project_path=str(project), settings=settings
        )
    finally:
        tracker.close_connection()

    assert captured["project_context"] == "COMPRESSED"
    assert fake_entroly_package["seen"]["budget"] == 777


def test_compression_falls_back_to_the_uncompressed_context_on_any_error(
    installed, fake_entroly_package
):
    fake_entroly_package["error"] = RuntimeError("entroly exploded")
    context = "# CLAUDE.md\nthe original, uncompressed context\n"
    assert entroly.compress_context(context, query="q") == context
    # It really tried: the fallback is a recovery, not a skipped call.
    assert fake_entroly_package["seen"]["query"] == "q"


def test_compression_falls_back_when_the_package_is_not_installed(
    installed, monkeypatch
):
    monkeypatch.setitem(sys.modules, "entroly", None)  # import raises ImportError
    context = "the original context"
    assert entroly.compress_context(context) == context


# --- criterion 5: agents are offered entroly as an MCP server -------------


def test_agents_receive_entroly_as_an_mcp_server(installed):
    from eng_crew.providers.claude_cli import ClaudeCLIProvider

    servers = entroly.mcp_servers("/some/project")
    assert "entroly" in servers
    assert servers["entroly"]["env"]["ENTROLY_PROJECT_PATH"] == "/some/project"

    cmd = ClaudeCLIProvider()._build_cmd("claude-sonnet-5", "p", {"cwd": "/some/project"})
    assert "--mcp-config" in cmd
    config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert "entroly" in config["mcpServers"]

    # A sandboxed session (the ticket reviewer) still gets no MCP servers.
    sandboxed = ClaudeCLIProvider()._build_cmd(
        "claude-sonnet-5", "p", {"cwd": "/some/project", "no_mcp": True}
    )
    config = json.loads(sandboxed[sandboxed.index("--mcp-config") + 1])
    assert config["mcpServers"] == {}


def test_agents_receive_no_entroly_mcp_server_when_it_is_off():
    from eng_crew.providers.claude_cli import ClaudeCLIProvider

    cmd = ClaudeCLIProvider()._build_cmd("claude-sonnet-5", "p", {"cwd": "."})
    config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert "entroly" not in config["mcpServers"]


# --- criterion 6: the status endpoint reports the real values -------------


def test_status_endpoint_reports_the_real_values(monkeypatch, tmp_path, installed):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from eng_crew import tracker

    monkeypatch.setenv("ENTROLY_QUALITY", "max")
    monkeypatch.setenv("ENTROLY_TOKEN_BUDGET", "4096")
    monkeypatch.setattr(tracker, "DB_PATH", tmp_path / "dash.db")
    tracker.close_connection()
    tracker._init_db()
    from eng_crew.dashboard.app import app

    try:
        with TestClient(app) as client:
            body = client.get("/api/entroly/status").json()
    finally:
        tracker.close_connection()

    assert body == {
        "enabled": True,
        "available": True,
        "quality": "max",
        "token_budget": 4096,
    }
