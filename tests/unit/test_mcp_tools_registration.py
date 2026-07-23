"""Unit tests asserting the expected MCP tool set registers correctly."""

from __future__ import annotations

from querygate.mcp.server import create_mcp_server

_EXPECTED_TOOLS = {
    "list_connections",
    "list_tables",
    "describe_table",
    "search_catalog",
    "run_structured_queries",
    "run_structured_writes",
    "list_query_templates",
    "run_query_template",
    "search_querygate_guide",
    "get_querygate_guide_topic",
    "get_querygate_setup_checklist",
    "explain_querygate_config_field",
    "explain_querygate_error",
    "describe_my_querygate_access",
    "inspect_querygate_configuration",
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
