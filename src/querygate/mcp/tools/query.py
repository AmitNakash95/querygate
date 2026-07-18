"""MCP tools for structured, policy-enforced reads — the only way to query data.

Note: deliberately does NOT use `from __future__ import annotations` — see
mcp/tools/connections.py for why.
"""

from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from querygate.execution.service import StructuredQueryService
from querygate.mcp.auth import get_mcp_caller
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_batch_size

_CONNECTION_FIELD = Field(description="Connection id from list_connections.")


def _service(connection: str) -> StructuredQueryService:
    caller = get_mcp_caller()
    return StructuredQueryService(connection_id=connection, principal=caller, surface="mcp")


class StructuredQueryToolResult(BaseModel):
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    limit: int
    offset: int


class ExplainToolResult(BaseModel):
    sql: str
    params: Optional[str] = None
    tables: List[str]
    limit: int


class BatchQueryItemToolResult(BaseModel):
    rows: Optional[List[Dict[str, Any]]] = None
    row_count: Optional[int] = None
    truncated: Optional[bool] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    error: Optional[str] = None


class BatchQueryToolResult(BaseModel):
    results: List[BatchQueryItemToolResult]


@mcp_server.tool(
    description=(
        "Execute a read-only structured query against the given connection. Supports "
        "multi-column select, inner/left joins, nested and/or filters (eq, neq, lt, lte, "
        "gt, gte, in, not_in, like, between, is_null, is_not_null), group_by, having, "
        "order_by, limit/offset, aggregate and date_bucket select items, and top_n "
        "per-partition ranking. Raw SQL is not accepted — every identifier is validated "
        "against the live reflected schema and the active policy before compilation. "
        "A join may target a DIFFERENT connection than the primary `connection` argument "
        "via the join's own `connection` field — only when both connections share the "
        "same policy join_group; otherwise run one call per connection and merge results "
        "yourself. "
        "order_by is a list of {col, dir} objects — dir must be the exact string 'asc' or "
        "'desc'; any other spelling is rejected with a validation error rather than being "
        "silently ignored. "
        "Select items may be a Table.Column string, an aggregate "
        "({fn: count|sum|avg|min|max, col, as}), or a date_bucket "
        "({col, granularity: day|week|month|quarter|year, as}) for time-bucketed trends. "
        "Optional top_n ({partition_by, order_by, n, fn: row_number|rank|dense_rank}) ranks "
        "rows within each partition and keeps only the top n — use for 'top N per group' "
        "asks. Row cap is tiered by policy: a lower limit for plain row selects, a higher "
        "one when the query has group_by or an aggregate select item. "
        "Always set intent to a short plain-language summary of the user's ask — logged "
        "with the compiled SQL for audit, never returned to the caller. "
        "Use explain_structured_query first to sanity-check an expensive-looking query. "
        "Queries run under a server-side execution timeout and a per-connection "
        "concurrency cap — a 'too many concurrent queries' error means wait briefly and "
        "retry once, not retry in a tight loop. Use list_tables and describe_table to "
        "discover valid identifiers first."
    )
)
@safe_mcp_tool
async def execute_structured_query(
    connection: Annotated[str, _CONNECTION_FIELD],
    query: Annotated[StructuredQuery, Field(description="Structured query AST")],
) -> Union[StructuredQueryToolResult, MCPErrorResult]:
    service = _service(connection)
    result = await service.execute(query)
    return StructuredQueryToolResult(**result.model_dump())


@mcp_server.tool(
    description=(
        "Validate and compile a StructuredQuery WITHOUT executing it — returns the SQL "
        "text (and bind params, if literal-binding wasn't possible) that "
        "execute_structured_query would run. Use this before running an "
        "expensive-looking query (wide joins, weak filters) to sanity-check it, or to "
        "debug a validation error without spending a real DB round trip."
    )
)
@safe_mcp_tool
async def explain_structured_query(
    connection: Annotated[str, _CONNECTION_FIELD],
    query: Annotated[
        StructuredQuery,
        Field(description="Structured query AST to validate and compile"),
    ],
) -> Union[ExplainToolResult, MCPErrorResult]:
    service = _service(connection)
    result = await service.explain(query)
    return ExplainToolResult(**result.model_dump())


@mcp_server.tool(
    description=(
        "Run several StructuredQuery objects against one connection in a single MCP call "
        "(still one DB round trip per query, but a single tool call) — subject to a "
        "per-connection max batch size set by policy. Prefer this over several separate "
        "execute_structured_query calls when you already know you need multiple "
        "vertical-slice queries. A cross-connection analytical ask spanning multiple "
        "connections still needs one call per connection — this batches multiple queries "
        "within a single connection, it does not span connections. "
        "One failing query in the batch does not fail the others — check each result's "
        "'error' field."
    )
)
@safe_mcp_tool
async def execute_structured_queries(
    connection: Annotated[str, _CONNECTION_FIELD],
    queries: Annotated[
        List[StructuredQuery],
        Field(description="List of structured query ASTs to run in one call"),
    ],
) -> Union[BatchQueryToolResult, MCPErrorResult]:
    caller = get_mcp_caller()
    validate_batch_size(len(queries), get_policy(connection, principal=caller))
    service = _service(connection)
    results = await service.execute_many(queries)
    return BatchQueryToolResult(
        results=[BatchQueryItemToolResult(**r.model_dump()) for r in results]
    )
