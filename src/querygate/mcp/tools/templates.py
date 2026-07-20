"""MCP tools for admin-defined query templates (TODO.md item 48).

A query template is a named, parameterized `StructuredQuery` an admin has
reviewed and published; agents invoke one by id with typed parameters instead
of composing an arbitrary query. Raw SQL is not accepted here either — a
template is a stored AST, and the bound result runs through the same policy/
schema/guardrail pipeline as any structured query.

Note: deliberately does NOT use `from __future__ import annotations` — see
mcp/tools/connections.py for why.
"""

from typing import Annotated, Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field

from querygate.connections.visibility import resolve_visible_connection
from querygate.core.exceptions import NotFoundError
from querygate.execution.admission import QueueMode
from querygate.execution.service import StructuredQueryService
from querygate.mcp.auth import get_mcp_caller
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server
from querygate.templates.binding import bind_template
from querygate.templates.loader import get_template_store
from querygate.templates.models import PublicQueryTemplate

_QUEUE_MODE_FIELD = Field(
    default=None,
    description=(
        "fail_fast: don't wait for a concurrency slot at all, reject immediately if the "
        "connection is at capacity. wait (default): wait up to wait_timeout_seconds, or the "
        "policy's own concurrency_wait_seconds ceiling if wait_timeout_seconds is omitted."
    ),
)
_WAIT_TIMEOUT_FIELD = Field(
    default=None,
    ge=0,
    description="Caller-requested wait (seconds) for a concurrency slot; clamped to policy.",
)


class QueryTemplateListToolResult(BaseModel):
    templates: List[PublicQueryTemplate]


class QueryTemplateRunToolResult(BaseModel):
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    limit: Optional[int] = None
    offset: int = 0
    admission_id: Optional[str] = None
    queue_wait_ms: Optional[int] = None


@mcp_server.tool(
    description=(
        "List the curated, named, parameterized query templates you may invoke — the finite "
        "set of pre-approved questions this caller can ask, filtered to those whose target "
        "connection is visible to you. Each returns its id, connection, description, and "
        "typed parameter signature. Call run_query_template with an id and parameters."
    )
)
@safe_mcp_tool
async def list_query_templates() -> Union[QueryTemplateListToolResult, MCPErrorResult]:
    caller = get_mcp_caller()
    visible: List[PublicQueryTemplate] = []
    for template in get_template_store().list():
        try:
            resolve_visible_connection(template.connection, principal=caller)
        except NotFoundError:
            continue
        visible.append(PublicQueryTemplate.from_template(template))
    return QueryTemplateListToolResult(templates=visible)


@mcp_server.tool(
    description=(
        "Invoke a curated query template by id with typed parameters (see "
        "list_query_templates for ids and parameter signatures). Parameters are validated "
        "against the template's typed slots, bound into the stored StructuredQuery, and the "
        "result runs through the same policy/schema/guardrail checks as an ad-hoc query — a "
        "template can never exceed the caller's policy. Returns rows like a structured query. "
        "Raw SQL is not accepted; a template is a stored AST, not a SQL string."
    )
)
@safe_mcp_tool
async def run_query_template(
    template_id: Annotated[str, Field(description="Template id from list_query_templates.")],
    parameters: Annotated[
        Optional[Dict[str, Any]],
        Field(default=None, description="Typed values for the template's parameter slots."),
    ] = None,
    queue_mode: Annotated[Optional[QueueMode], _QUEUE_MODE_FIELD] = None,
    wait_timeout_seconds: Annotated[Optional[float], _WAIT_TIMEOUT_FIELD] = None,
) -> Union[QueryTemplateRunToolResult, MCPErrorResult]:
    caller = get_mcp_caller()
    supplied = parameters or {}
    template = get_template_store().get(template_id)
    # Uniform not-found whether the template is unknown or its connection is
    # hidden from this caller — never an enumeration oracle.
    if template is None:
        raise NotFoundError(f"Unknown query template: {template_id!r}")
    try:
        resolve_visible_connection(template.connection, principal=caller)
    except NotFoundError:
        raise NotFoundError(f"Unknown query template: {template_id!r}")
    service = StructuredQueryService(
        connection_id=template.connection,
        principal=caller,
        surface="mcp",
        template_id=template.id,
        template_param_shape=sorted(supplied),
    )
    query = bind_template(template, supplied)
    result = await service.execute(
        query, queue_mode=queue_mode, wait_timeout_seconds=wait_timeout_seconds
    )
    return QueryTemplateRunToolResult(**result.model_dump())
