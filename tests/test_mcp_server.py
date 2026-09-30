"""The MCP server must keep working across MCP SDK generations.

mcp 2.0 renamed the high-level server class (mcp.server.fastmcp.FastMCP ->
mcp.server.mcpserver.MCPServer). mcp_server.py imports whichever the installed
SDK provides; these tests assert that the tool surface is intact either way, so
an SDK upgrade can't silently take the dispatch entrypoint offline.
"""
from __future__ import annotations

import asyncio

import pytest

mcp_server = pytest.importorskip(
    "eng_crew.mcp_server", reason="mcp SDK not installed (optional [mcp] extra)"
)

EXPECTED_TOOLS = {
    "list_projects",
    "resume_run",
    "run_task",
    "services_status",
    "start_services",
    "stop_services",
}


def _list_tools():
    result = mcp_server.mcp.list_tools()
    # 1.x and 2.x both expose list_tools as a coroutine; tolerate either.
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def test_server_object_is_the_sdks_high_level_server():
    assert type(mcp_server.mcp).__name__ in ("FastMCP", "MCPServer")


def test_server_is_named_project_starter():
    # The name clients bind to — changing it silently breaks every caller.
    assert mcp_server.mcp.name == "project-starter"


def test_every_tool_is_registered():
    assert {t.name for t in _list_tools()} == EXPECTED_TOOLS


def test_tools_carry_descriptions():
    # Descriptions come from docstrings and are what the model routes on.
    for tool in _list_tools():
        assert tool.description, f"{tool.name} has no description"


def test_run_task_takes_project_path_and_task():
    tool = next(t for t in _list_tools() if t.name == "run_task")
    schema = getattr(tool, "input_schema", None) or tool.inputSchema  # 2.x / 1.x
    assert set(schema["properties"]) == {
        "project_path", "task", "acceptance_criteria", "base_branch",
    }
    # Everything but the task stays optional: a caller that does not care about
    # stacking, and one with no criteria of its own (the Manager drafts those),
    # both keep dispatching with two arguments.
    assert set(schema.get("required", [])) == {"project_path", "task"}


def test_run_task_dispatches_through_a_ticket(monkeypatch):
    """The tool is a thin wrapper over dispatch_task — a task is a ticket now."""
    from eng_crew import dispatch

    seen = {}

    def fake_dispatch(project_path, task, acceptance_criteria="", **kwargs):
        seen.update(project_path=project_path, task=task,
                    criteria=acceptance_criteria, **kwargs)
        return dispatch.Dispatch(
            True, "building ticket 7", ticket_id=7, started=True,
            criteria_source="given", wip={"in_flight": 1, "limit": 3, "slots": 2},
        )

    monkeypatch.setattr(dispatch, "dispatch_task", fake_dispatch)

    out = mcp_server.run_task(".", "add pagination", "- 20 rows")

    assert seen["task"] == "add pagination"
    assert seen["criteria"] == "- 20 rows"
    assert "#7" in out and "1/3 in flight" in out


def test_run_task_reports_a_ticket_that_was_filed_but_not_started(monkeypatch):
    from eng_crew import dispatch

    monkeypatch.setattr(
        dispatch, "dispatch_task",
        lambda *a, **k: dispatch.Dispatch(
            True, "WIP limit reached (3/3 in flight) — ticket 9 is ready and waiting",
            ticket_id=9, started=False, criteria_source="manager",
        ),
    )

    out = mcp_server.run_task(".", "add pagination")

    assert "not started" in out
    assert "WIP limit reached" in out
