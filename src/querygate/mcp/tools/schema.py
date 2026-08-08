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

# TODO.md item 130: `x-mcp-header` annotation, mirrored into
# `Mcp-Param-Connection` for a fronting gateway — see mcp/tools/query.py's
# `_CONNECTION_FIELD` for the full rationale and the join-connection caveat.
_CONNECTION_FIELD = Field(
    description="Connection id from list_connections.",
    json_schema_extra={"x-mcp-header": "Connection"},
)
_VERBOSE_PROVENANCE_FIELD = Field(
    default=False,
    description=(
        "False (default): each catalog citation is compact (status + precedence only). "
        "True: full citation (entry id, evidence, confidence, catalog/schema version, "
        "freshness) — request only when you need to audit catalog trust in detail."
    ),
)


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


class CatalogSearchToolResult(BaseModel):
    query: str
    results: List[Dict[str, Any]]
    result_count: int
    truncated: bool
    max_results: int
    max_response_bytes: int


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
        "catalog configured; null when it doesn't. A catalog object's provenance is "
        "compact by default — set verbose_provenance for the full citation. Use to "
        "discover valid Table.Column references for structured queries."
    )
)
@safe_mcp_tool
async def describe_table(
    connection: Annotated[str, _CONNECTION_FIELD],
    table_name: Annotated[str, Field(min_length=1, description="Table name")],
    verbose_provenance: Annotated[bool, _VERBOSE_PROVENANCE_FIELD] = False,
) -> Union[TableDescribeToolResult, MCPErrorResult]:
    service = _service(connection)
    description = await service.describe_table(table_name, verbose_provenance=verbose_provenance)
    return TableDescribeToolResult(
        name=description.name,
        columns=[c.model_dump() for c in description.columns],
        description=description.description,
        catalog=description.catalog.model_dump() if description.catalog else None,
    )


@mcp_server.tool(
    description=(
        "Search compact business metadata in one connection's semantic catalog. Policy is "
        "applied before search and ranking, so denied tables, columns, and relationship "
        "endpoints cannot influence hits or counts. Every hit includes a citation — compact "
        "(status + precedence) by default, or the full provenance (verification state, "
        "confidence, catalog/schema version, freshness) when verbose_provenance is set. "
        "This reads metadata only; it never searches database rows or executes SQL."
    )
)
@safe_mcp_tool
async def search_catalog(
    connection: Annotated[str, _CONNECTION_FIELD],
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=256,
            description="Business term, table/column alias, or relationship concept.",
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=20, description="Maximum compact hits.")] = 5,
    verbose_provenance: Annotated[bool, _VERBOSE_PROVENANCE_FIELD] = False,
) -> Union[CatalogSearchToolResult, MCPErrorResult]:
    service = _service(connection)
    response = await service.search_catalog(
        query, max_results=limit, verbose_provenance=verbose_provenance
    )
    return CatalogSearchToolResult(
        query=response.query,
        results=[result.model_dump(mode="json") for result in response.results],
        result_count=response.result_count,
        truncated=response.truncated,
        max_results=response.max_results,
        max_response_bytes=response.max_response_bytes,
    )
