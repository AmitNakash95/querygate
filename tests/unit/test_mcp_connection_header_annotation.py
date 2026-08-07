"""TODO.md item 130: `connection` carries the MCP `2026-07-28` spec's
`x-mcp-header` JSON-schema annotation so a fronting gateway can mirror it into
an `Mcp-Param-Connection` HTTP header and route/authorize per-connection
without parsing the request body — without QueryGate's server itself needing
to speak that protocol revision yet (the annotation is inert extra JSON
Schema metadata on `inputSchema`, forward-compatible with any future
transport/SDK upgrade).

Two invariants, asserted against the *actual emitted* tool schemas from a
real `create_mcp_server()` instance (never just source grepping, since the
annotation could be silently dropped anywhere in FastMCP's pydantic ->
JSON-schema pipeline):

1. Every tool that takes a `connection` parameter annotates it with
   `x-mcp-header: "Connection"`.
2. No other parameter carries an `x-mcp-header` annotation at all — item
   130's explicit scope is `connection` only. Checked at both levels a tool's
   `inputSchema` can carry a property: the tool's own top-level arguments
   (`mode`, `verbose_provenance`, ...) AND every query/write AST field nested
   under `$defs` (`ColumnExpr.col`, `CaseExpr.when`, `InsertStatement.rows`,
   ...) — pydantic's `model_json_schema()` hoists every referenced submodel
   there rather than inlining it, so a check that only walks top-level
   `properties` would silently miss an annotation added to an AST field.
   Mirroring query/write AST internals into headers would leak query
   semantics to intermediaries and invert the confidentiality posture
   (CLAUDE.md's engine-philosophy / non-negotiable-8 framing applies here
   too: expose the one primitive the spec blesses, don't spoon-feed a
   gateway more).
"""

from __future__ import annotations

from querygate.mcp.server import create_mcp_server

# Tools with a top-level `connection` argument, per mcp/tools/{query,schema,
# write}.py — kept in sync with test_mcp_tools_registration.py's full tool
# roster; a tool NOT in this set must have no `connection` property at all
# (asserted below), and a tool IN this set must have it annotated.
_TOOLS_WITH_CONNECTION_PARAM = {
    "run_structured_queries",
    "list_tables",
    "describe_table",
    "search_catalog",
    "run_structured_writes",
}

_HEADER_KEY = "x-mcp-header"
_EXPECTED_HEADER_NAME = "Connection"


def _all_tool_schemas():
    server = create_mcp_server()
    return {name: tool.parameters for name, tool in server._tool_manager._tools.items()}


def _iter_non_root_properties(schema: dict):
    """Yield (location, prop_name, prop_schema) for every property NOT on the
    schema root — i.e. every field of every query/write AST type nested under
    `$defs` (`ColumnExpr`, `CaseExpr`, `InsertStatement`, ... — pydantic's
    `model_json_schema()` hoists every referenced submodel there, not inline).

    A tool's top-level `inputSchema.properties` is only ever `connection` plus
    a handful of scalar/enum tool arguments (`mode`, `queue_mode`, ...); the
    entire `StructuredQuery`/write-AST surface — the thing item 130's scope
    note explicitly says must never be annotated — lives here instead. Without
    walking `$defs` too, a future `x-mcp-header` added to e.g. `ColumnExpr.col`
    would pass every check below unnoticed.
    """
    for def_name, def_schema in schema.get("$defs", {}).items():
        for prop_name, prop_schema in def_schema.get("properties", {}).items():
            yield f"$defs.{def_name}", prop_name, prop_schema


def test_every_connection_param_carries_the_header_annotation():
    schemas = _all_tool_schemas()
    seen = set()
    for name, schema in schemas.items():
        properties = schema.get("properties", {})
        if "connection" not in properties:
            continue
        seen.add(name)
        connection_prop = properties["connection"]
        assert connection_prop.get(_HEADER_KEY) == _EXPECTED_HEADER_NAME, (
            f"{name}'s `connection` parameter is missing the x-mcp-header "
            f"annotation (or has the wrong value): {connection_prop!r}"
        )
    # Fails closed if a tool gains/loses a `connection` param and this test's
    # roster silently drifts out of sync with reality.
    assert seen == _TOOLS_WITH_CONNECTION_PARAM


def test_no_parameter_other_than_connection_carries_the_header_annotation():
    """Guards item 130's "scope: `connection` only" rule at BOTH levels a
    tool's inputSchema can carry a property: the top-level tool arguments
    (`mode`, `verbose_provenance`, ...) and every nested query/write AST field
    reachable through `$defs` (`ColumnExpr.col`, `CaseExpr.when`, ...). The
    AST case is the one item 130's own rationale names as the actual risk —
    mirroring query/write semantics into a header — so it isn't optional
    coverage, it's the point of this test.
    """
    schemas = _all_tool_schemas()
    for name, schema in schemas.items():
        for prop_name, prop_schema in schema.get("properties", {}).items():
            if prop_name == "connection":
                continue
            assert _HEADER_KEY not in prop_schema, (
                f"{name}.{prop_name} unexpectedly carries x-mcp-header "
                f"({prop_schema.get(_HEADER_KEY)!r}) — item 130's scope is "
                "`connection` only; mirroring query/write AST internals into "
                "headers would leak query semantics to intermediaries."
            )
        for location, prop_name, prop_schema in _iter_non_root_properties(schema):
            assert _HEADER_KEY not in prop_schema, (
                f"{name}'s {location}.{prop_name} unexpectedly carries "
                f"x-mcp-header ({prop_schema.get(_HEADER_KEY)!r}) — item 130's "
                "scope is `connection` only; mirroring query/write AST "
                "internals into headers would leak query semantics to "
                "intermediaries."
            )


def test_tools_without_a_connection_param_have_no_header_annotation_anywhere():
    schemas = _all_tool_schemas()
    for name, schema in schemas.items():
        if name in _TOOLS_WITH_CONNECTION_PARAM:
            continue
        for prop_name, prop_schema in schema.get("properties", {}).items():
            assert _HEADER_KEY not in prop_schema, (
                f"{name}.{prop_name} carries x-mcp-header but {name} isn't a "
                "connection-taking tool"
            )
