"""Unit tests asserting the expected MCP tool set registers correctly."""

from __future__ import annotations

from querygate.mcp.server import create_mcp_server

_EXPECTED_TOOLS = {
    "list_connections",
    "list_tables",
    "describe_table",
    "explain_structured_query",
    "execute_structured_query",
    "execute_structured_queries",
}


def test_expected_tools_are_registered():
    server = create_mcp_server()
    tool_names = set(server._tool_manager._tools.keys())
    assert _EXPECTED_TOOLS.issubset(tool_names)


def test_no_raw_sql_tool_registered():
    server = create_mcp_server()
    tool_names = set(server._tool_manager._tools.keys())
    for name in tool_names:
        assert "sql" not in name.lower() or name in _EXPECTED_TOOLS


def test_server_name_is_querygate():
    server = create_mcp_server()
    assert server.name == "querygate"
