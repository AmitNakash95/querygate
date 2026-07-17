"""MCP tool: discover which connections this deployment exposes.

Note: deliberately does NOT use `from __future__ import annotations` — FastMCP
resolves each tool's forward-referenced annotations against the *wrapping*
function's `__globals__` (the `safe_mcp_tool` decorator's module), not this
module's, so stringified annotations here would fail to resolve at
registration time. Keeping annotations as real objects sidesteps that.
"""

from typing import List, Union

import pydantic as pyd

from querygate.connections.models import PublicConnectionInfo
from querygate.connections.registry import get_registry
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server


class ConnectionsListToolResult(pyd.BaseModel):
    connections: List[PublicConnectionInfo]


@mcp_server.tool(
    description=(
        "List every database connection this deployment exposes (id, SQL dialect, "
        "enabled state, description). Never returns credentials — there is no field or "
        "tool anywhere that can. Call this first: every other tool takes a `connection` "
        "id from this list."
    )
)
@safe_mcp_tool
async def list_connections() -> Union[ConnectionsListToolResult, MCPErrorResult]:
    return ConnectionsListToolResult(connections=get_registry().list_public())
