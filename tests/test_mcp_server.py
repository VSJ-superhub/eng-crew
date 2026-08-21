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
    assert set(schema["properties"]) == {"project_path", "task"}
    assert set(schema.get("required", [])) == {"project_path", "task"}
