"""MCP tools for structured, policy-enforced reads — the only way to query data.

Note: deliberately does NOT use `from __future__ import annotations` — see
mcp/tools/connections.py for why.
"""

from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from querygate.execution.admission import QueueMode, reject_unsupported_async_queue_mode
from querygate.execution.service import StructuredQueryService
from querygate.mcp.auth import get_mcp_caller, get_mcp_config
from querygate.mcp.elicitation import build_elicitation_resolver
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_batch_size

_CONNECTION_FIELD = Field(description="Connection id from list_connections.")
_QUEUE_MODE_FIELD = Field(
    default=None,
    description=(
        "fail_fast: don't wait for a concurrency slot at all, reject immediately if the "
        "connection is at capacity. wait (default): wait up to wait_timeout_seconds, or the "
        "policy's own concurrency_wait_seconds ceiling if wait_timeout_seconds is omitted. "
        "async is REST-only (the single-query POST .../query endpoint's 202/poll/cancel "
        "lifecycle) and is rejected here as a validation error."
    ),
)
_WAIT_TIMEOUT_FIELD = Field(
    default=None,
    ge=0,
    description=(
        "Caller-requested wait (seconds) for a concurrency slot. Clamped to the operator's "
        "policy concurrency_wait_seconds ceiling — a caller may request a shorter wait, "
        "never a longer one."
    ),
)


def _service(connection: str) -> StructuredQueryService:
    caller = get_mcp_caller()
    return StructuredQueryService(connection_id=connection, principal=caller, surface="mcp")


class BatchQueryItemToolResult(BaseModel):
    rows: Optional[List[Dict[str, Any]]] = None
    row_count: Optional[int] = None
    truncated: Optional[bool] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    admission_id: Optional[str] = None
    admission_state: Optional[str] = None
    queue_wait_ms: Optional[int] = None
    error: Optional[str] = None


class BatchQueryToolResult(BaseModel):
    results: List[BatchQueryItemToolResult]


class BatchExplainItemToolResult(BaseModel):
    sql: Optional[str] = None
    params: Optional[str] = None
    tables: Optional[List[str]] = None
    limit: Optional[int] = None
    error: Optional[str] = None


class BatchExplainToolResult(BaseModel):
    results: List[BatchExplainItemToolResult]


@mcp_server.tool(
    description=(
        "Run or dry-run one or more read-only StructuredQuery objects against one "
        "connection — always pass queries as a list, even for a single query; results "
        "are always a list in the same order, one item per query, and one failing query "
        "never fails the others (check each item's 'error' field). mode='execute' "
        "(default) runs each query for real and returns rows. mode='explain' validates "
        "and compiles WITHOUT executing — returns the SQL (and bind params) that would "
        "run, with no database round trip and no concurrency-limiter interaction; use it "
        "before running an expensive-looking query (wide joins, weak filters) or to debug "
        "a validation error. Supports multi-column select, inner/left/full/cross joins "
        "(an equality `on` pair or a general `condition` predicate tree for range joins), nested "
        "and/or filters (eq, neq, lt, lte, gt, gte, in, not_in, like, between, is_null, "
        "is_not_null), group_by, having, order_by, limit/offset, aggregate and date_bucket "
        "select items, and top_n per-partition ranking — see the StructuredQuery field "
        "schema for each field's exact contract (order_by.dir, a join's cross-connection "
        "connection field, date_bucket granularity, top_n's fn options, intent, etc). Raw "
        "SQL is not accepted — every identifier is validated against the live reflected "
        "schema and the active policy before compilation. A cross-connection analytical "
        "ask spanning multiple connections still needs one call per connection. Row cap "
        "is tiered by policy: a lower limit for plain row selects, a higher one when a "
        "query has group_by or an aggregate select item. mode='execute' queries run under "
        "a server-side execution timeout and a per-connection concurrency cap — a 'too "
        "many concurrent queries' error means wait briefly and retry once, not in a tight "
        "loop; queue_mode/wait_timeout_seconds below control the wait (ignored in "
        "mode='explain'). Use list_tables/describe_table to discover valid identifiers "
        "first. Example query (region totals, paid or high-priority orders over $100, "
        "top region first): "
        '{"from": "Order", "select": ["Customer.Region", '
        '{"fn": "sum", "col": "Order.Total", "as": "total"}], '
        '"joins": [{"table": "Customer", "on": ["Order.CustomerId", "Customer.Id"]}], '
        '"where": {"and": [{"col": "Order.Status", "op": "eq", "value": "paid"}, '
        '{"or": [{"col": "Order.Total", "op": "gte", "value": 100}, '
        '{"col": "Order.Priority", "op": "eq", "value": "high"}]}]}, '
        '"group_by": ["Customer.Region"], '
        '"order_by": [{"col": "total", "dir": "desc"}], "limit": 10}'
    )
)
@safe_mcp_tool
async def run_structured_queries(
    connection: Annotated[str, _CONNECTION_FIELD],
    queries: Annotated[
        List[StructuredQuery],
        Field(min_length=1, description="One or more structured query ASTs to run, in order."),
    ],
    mode: Annotated[
        Literal["execute", "explain"],
        Field(description="'execute' runs queries for real; 'explain' validates/compiles only."),
    ] = "execute",
    queue_mode: Annotated[Optional[QueueMode], _QUEUE_MODE_FIELD] = None,
    wait_timeout_seconds: Annotated[Optional[float], _WAIT_TIMEOUT_FIELD] = None,
    ctx: Context = None,
) -> Union[BatchQueryToolResult, BatchExplainToolResult, MCPErrorResult]:
    reject_unsupported_async_queue_mode(queue_mode)
    caller = get_mcp_caller()
    validate_batch_size(len(queries), get_policy(connection, principal=caller))
    service = _service(connection)
    if mode == "explain":
        explain_results = await service.explain_many(queries)
        return BatchExplainToolResult(
            results=[BatchExplainItemToolResult(**r.model_dump()) for r in explain_results]
        )
    # In-query human-in-the-loop approval (item 92): if a query trips the gate,
    # ask the client's human to approve it in-session via elicitation instead of
    # the out-of-band REST token flow. Opt-in and off by default (see
    # AppConfig.mcp_elicitation_approval_enabled); when off, resolver is None and
    # a gated query stays fail-closed as that item's error.
    resolver = (
        build_elicitation_resolver(ctx, caller, get_mcp_config()) if ctx is not None else None
    )
    # Progress notifications (TODO.md item 35 phase 3): MCP's standard
    # notifications/progress message, via FastMCP's Context.report_progress —
    # a no-op when the client sent no progressToken, so this is unconditional
    # and purely additive. Two points per query (wait start, admitted), not a
    # continuous tick during the wait — see execute()'s own docstring for why.
    on_wait_start = on_admitted = None
    if ctx is not None:

        async def on_wait_start(wait_seconds: float) -> None:
            message = (
                f"waiting up to {wait_seconds:.0f}s for a concurrency slot"
                if wait_seconds > 0
                else "submitting query"
            )
            await ctx.report_progress(0, max(wait_seconds, 1), message)

        async def on_admitted(queue_wait_ms: int) -> None:
            await ctx.report_progress(
                queue_wait_ms / 1000, max(queue_wait_ms / 1000, 1), "admitted, executing"
            )

    results = await service.execute_many(
        queries,
        queue_mode=queue_mode,
        wait_timeout_seconds=wait_timeout_seconds,
        approval_resolver=resolver,
        on_wait_start=on_wait_start,
        on_admitted=on_admitted,
    )
    return BatchQueryToolResult(
        results=[BatchQueryItemToolResult(**r.model_dump()) for r in results]
    )
