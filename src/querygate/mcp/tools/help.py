"""MCP tools for the packaged product guide and caller-scoped diagnostics.

No future-annotations import here; see mcp/tools/connections.py.
"""

from typing import Annotated, Literal, Union

from pydantic import Field

from querygate.help.models import (
    AccessSummary,
    ConfigFieldExplanation,
    ErrorExplanation,
    GuideSearchResponse,
    GuideTopicResponse,
    RedactedConfiguration,
    SetupChecklistResponse,
)
from querygate.help.service import get_guide_service
from querygate.mcp.auth import get_mcp_caller, get_mcp_config
from querygate.mcp.exceptions import MCPErrorResult, safe_mcp_tool
from querygate.mcp.server import mcp_server


@mcp_server.tool(
    description=(
        "Search QueryGate's canonical guide for version-correct installation, configuration, "
        "operation, interface, and troubleshooting guidance. This deterministic offline search "
        "uses packaged public documentation only; it never searches customer data or live config."
    )
)
@safe_mcp_tool
async def search_querygate_guide(
    query: Annotated[str, Field(min_length=1, max_length=200)],
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> Union[GuideSearchResponse, MCPErrorResult]:
    return get_guide_service().search(query, limit=limit)


@mcp_server.tool(
    description=(
        "Retrieve one complete canonical QueryGate guide topic by the topic_id returned from "
        "search_querygate_guide, including installed-version citation and safe next actions."
    )
)
@safe_mcp_tool
async def get_querygate_guide_topic(
    topic_id: Annotated[str, Field(min_length=1)],
) -> Union[GuideTopicResponse, MCPErrorResult]:
    return get_guide_service().topic(topic_id)


@mcp_server.tool(
    description=(
        "Return a versioned setup checklist for a local, container, or production QueryGate "
        "deployment. Works offline and does not contact configured databases."
    )
)
@safe_mcp_tool
async def get_querygate_setup_checklist(
    profile: Literal["local", "container", "production"] = "local",
) -> Union[SetupChecklistResponse, MCPErrorResult]:
    return get_guide_service().setup_checklist(profile)


@mcp_server.tool(
    description=(
        "Explain one static QueryGate configuration field from the app, connection, policy, "
        "or catalog model. Returns schema documentation and defaults, never live values."
    )
)
@safe_mcp_tool
async def explain_querygate_config_field(
    model: Literal["app", "connection", "policy", "catalog"],
    field: Annotated[str, Field(min_length=1)],
) -> Union[ConfigFieldExplanation, MCPErrorResult]:
    return get_guide_service().explain_config_field(model, field)


@mcp_server.tool(
    description=(
        "Explain a stable QueryGate error code with safe, version-correct next actions. The tool "
        "does not inspect raw exceptions, logs, arbitrary request ids, or another caller's state."
    )
)
@safe_mcp_tool
async def explain_querygate_error(
    error_code: Annotated[str, Field(min_length=1)],
) -> Union[ErrorExplanation, MCPErrorResult]:
    return get_guide_service().explain_error(error_code)


@mcp_server.tool(
    description=(
        "Describe only the authenticated caller's own QueryGate scopes, administrative "
        "capabilities, and policy-visible connections. Hidden and unknown resources remain "
        "indistinguishable."
    )
)
@safe_mcp_tool
async def describe_my_querygate_access() -> Union[AccessSummary, MCPErrorResult]:
    return get_guide_service().access_summary(get_mcp_caller())


@mcp_server.tool(
    description=(
        "Return a redacted summary of the active QueryGate configuration. Requires "
        "admin:config:read and omits connection strings, credential values/references, raw "
        "policy identifiers, principal subjects, catalog identifiers, free-form descriptions, "
        "and raw YAML."
    )
)
@safe_mcp_tool
async def inspect_querygate_configuration() -> Union[RedactedConfiguration, MCPErrorResult]:
    return get_guide_service().redacted_configuration(get_mcp_config(), get_mcp_caller())
