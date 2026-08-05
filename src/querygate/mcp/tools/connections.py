"""MCP tool: discover which connections this deployment exposes.

Note: deliberately does NOT use `from __future__ import annotations` —
`MCPServer` resolves each tool's forward-referenced annotations against the
*wrapping* function's `__globals__` (the `safe_mcp_tool` decorator's
module), not this module's, so stringified annotations here would fail to
resolve at registration time. Keeping annotations as real objects sidesteps
that.
"""

from typing import List, Union

import pydantic as pyd

from querygate.connections.models import PublicConnectionInfo
from querygate.connections.visibility import list_visible_connections
from querygate.mcp.auth import get_mcp_caller
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server


class ConnectionsListToolResult(pyd.BaseModel):
    connections: List[PublicConnectionInfo]


@mcp_server.tool(
    description=(
        "List database connections visible to the authenticated caller (id, SQL dialect, "
        "enabled state, description). Principal policy and deployment-level disabled "
        "connections are omitted. Never returns credentials — there is no field or tool "
        "anywhere that can. Call this first: every other tool takes a `connection` id "
        "from this list."
    )
)
@safe_mcp_tool
async def list_connections() -> Union[ConnectionsListToolResult, MCPErrorResult]:
    return ConnectionsListToolResult(connections=list_visible_connections(get_mcp_caller()))
