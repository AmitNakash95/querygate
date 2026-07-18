"""MCP tools for schema discovery.

Note: deliberately does NOT use `from __future__ import annotations` — see
mcp/tools/connections.py for why.
"""

from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from querygate.execution.service import StructuredQueryService
from querygate.mcp.auth import get_mcp_caller
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server

_CONNECTION_FIELD = Field(description="Connection id from list_connections.")


def _service(connection: str) -> StructuredQueryService:
    caller = get_mcp_caller()
    return StructuredQueryService(connection_id=connection, principal=caller, surface="mcp")


class TablesListToolResult(BaseModel):
    tables: List[str]


class TableDescribeToolResult(BaseModel):
    name: str
    columns: List[Dict[str, Any]]
    description: Optional[str] = None
    catalog: Optional[Dict[str, Any]] = None


@mcp_server.tool(
    description=(
        "List known table names for the given connection (reflected metadata + any "
        "seeded catalog on the connection, otherwise a live schema query), filtered to "
        "what policy allows. Use before building structured queries."
    )
)
@safe_mcp_tool
async def list_tables(
    connection: Annotated[str, _CONNECTION_FIELD],
) -> Union[TablesListToolResult, MCPErrorResult]:
    service = _service(connection)
    tables = await service.list_tables()
    return TablesListToolResult(tables=tables)


@mcp_server.tool(
    description=(
        "Describe columns for a table in the given connection (name, SQLAlchemy type "
        "string, nullable, and description when the source DB has a documented comment "
        "for that table/column — description may be null). Columns denied by policy are "
        "omitted from the result, not flagged as errors. Each column and the table itself "
        "may carry an optional 'catalog' object (business description, aliases, "
        "sensitivity class, allow_samples, and — table-level only — default_aggregation and "
        "relationship hints to other tables) when the deployment has a curated schema "
        "catalog configured; null when it doesn't. Use to discover valid Table.Column "
        "references for structured queries."
    )
)
@safe_mcp_tool
async def describe_table(
    connection: Annotated[str, _CONNECTION_FIELD],
    table_name: Annotated[str, Field(min_length=1, description="Table name")],
) -> Union[TableDescribeToolResult, MCPErrorResult]:
    service = _service(connection)
    description = await service.describe_table(table_name)
    return TableDescribeToolResult(
        name=description.name,
        columns=[c.model_dump() for c in description.columns],
        description=description.description,
        catalog=description.catalog.model_dump() if description.catalog else None,
    )
