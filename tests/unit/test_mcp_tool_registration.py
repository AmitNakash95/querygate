"""MCP tool registration is explicit, and stays in step with the directory.

`querygate.mcp.tools.TOOL_MODULES` replaced a `Path(__file__).parent.glob("*.py")`
scan after item 214's compiler spike measured what the scan does in a frozen
build: no `.py` files exist on disk, so it matched nothing,
`discover_and_register_tools()` returned 0, and the MCP server advertised **zero
tools** — exit 0, no exception, no warning. A server that starts fine and serves
no tools is the worst shape a failure can take, because nothing draws attention
to it.

The static tuple fixes that, and introduces one new way to be wrong: adding a
tool module and forgetting to register it. That is what the drift test below
exists for. Both directions are checked, because the missing-from-tuple
direction ships a tool nobody can call and the missing-from-disk direction is an
ImportError at startup.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from querygate.mcp import tools as tools_pkg

pytestmark = pytest.mark.unit


def _modules_on_disk() -> set[str]:
    directory = Path(tools_pkg.__file__).resolve().parent
    return {p.stem for p in directory.glob("*.py") if not p.name.startswith("_")}


def test_registered_tuple_matches_the_directory_listing():
    """Adding a tool file without registering it must fail here, not silently
    ship a tool that never appears in `tools/list`."""
    on_disk = _modules_on_disk()
    registered = set(tools_pkg.TOOL_MODULES)
    assert registered == on_disk, (
        "querygate.mcp.tools.TOOL_MODULES has drifted from the directory.\n"
        f"  on disk but not registered: {sorted(on_disk - registered)}\n"
        f"  registered but not on disk: {sorted(registered - on_disk)}"
    )


def test_the_tuple_is_sorted_and_has_no_duplicates():
    """Keeps diffs readable and makes a double-registration obvious."""
    assert list(tools_pkg.TOOL_MODULES) == sorted(tools_pkg.TOOL_MODULES)
    assert len(set(tools_pkg.TOOL_MODULES)) == len(tools_pkg.TOOL_MODULES)


#: The complete agent-facing MCP surface. Asserted as a SET, not a count: a
#: `>= len(TOOL_MODULES)` check has nine tools of slack, so every tool in
#: query/write/schema/templates/connections could vanish and `help`'s seven
#: alone would still satisfy it. The MCP surface is the agent-facing API, so a
#: tool silently appearing or disappearing is a scope change, not a nit.
EXPECTED_TOOLS = frozenset(
    {
        "describe_my_querygate_access",
        "describe_table",
        "explain_querygate_config_field",
        "explain_querygate_error",
        "get_querygate_guide_topic",
        "get_querygate_setup_checklist",
        "inspect_querygate_configuration",
        "list_connections",
        "list_query_templates",
        "list_tables",
        "run_query_template",
        "run_structured_queries",
        "run_structured_writes",
        "search_catalog",
        "search_querygate_guide",
    }
)


def test_discovery_actually_imports_the_modules():
    """Asserts the observable effect, not the return value.

    `discover_and_register_tools()` returns `len(TOOL_MODULES)` — comparing that
    to `len(TOOL_MODULES)` is true by construction, and would stay green with
    the import loop deleted entirely.
    """
    import sys

    for name in tools_pkg.TOOL_MODULES:
        sys.modules.pop(f"querygate.mcp.tools.{name}", None)
    tools_pkg.discover_and_register_tools()
    still_missing = [
        n for n in tools_pkg.TOOL_MODULES if f"querygate.mcp.tools.{n}" not in sys.modules
    ]
    assert not still_missing, f"discovery did not import: {still_missing}"


def test_the_server_actually_advertises_tools():
    """The end-to-end assertion the spike wished existed.

    `discover_and_register_tools()` returning a positive count is necessary but
    not sufficient — registration could import cleanly and still register
    nothing. Assert against the surface an MCP client actually sees.
    """
    from querygate.mcp.server import create_mcp_server

    server = create_mcp_server()
    advertised = {t.name for t in asyncio.run(server.list_tools())}
    assert advertised == EXPECTED_TOOLS, (
        f"the advertised MCP surface changed.\n"
        f"  missing: {sorted(EXPECTED_TOOLS - advertised)}\n"
        f"  unexpected: {sorted(advertised - EXPECTED_TOOLS)}\n"
        "If deliberate, update EXPECTED_TOOLS — this set is the agent-facing API."
    )


def test_typed_tools_still_resolve_their_argument_schemas():
    """Guards the forward-reference resolution CLAUDE.md warns about.

    A tool whose annotations failed to resolve registers with an empty
    `properties`, which no count-based check would notice.
    """
    from querygate.mcp.server import create_mcp_server

    server = create_mcp_server()
    by_name = {t.name: t for t in asyncio.run(server.list_tools())}
    for name in ("run_structured_queries", "describe_table", "search_catalog"):
        assert name in by_name, f"{name} is not advertised"
        schema = getattr(by_name[name], "input_schema", None) or {}
        assert schema.get("properties"), f"{name} advertised an empty argument schema"


def test_the_ast_rebuild_runs_before_any_tool_module_is_imported():
    """The ordering the module docstring calls load-bearing, and which had no test.

    `discover_and_register_tools()` must call `rebuild_recursive_ast_cycle(force=True)`
    *before* importing any tool module: registration builds each tool's JSON
    schema over the recursive model graph, and a stale graph was observed
    misattaching a `$ref`'s sibling `description`. That defect is invisible to
    every count- or presence-based check — the schema is present, just subtly
    wrong — so without this test, hoisting the imports to module scope (the
    obvious "fix" for the compiler problem) breaks the guarantee silently.
    """
    import sys

    order: list[str] = []
    real_rebuild = tools_pkg.rebuild_recursive_ast_cycle

    def recording_rebuild(*args, **kwargs):
        order.append("rebuild")
        return real_rebuild(*args, **kwargs)

    for name in tools_pkg.TOOL_MODULES:
        sys.modules.pop(f"querygate.mcp.tools.{name}", None)

    original = tools_pkg.rebuild_recursive_ast_cycle
    tools_pkg.rebuild_recursive_ast_cycle = recording_rebuild
    try:
        real_import = tools_pkg.importlib.import_module

        def recording_import(name, *args, **kwargs):
            if name.startswith("querygate.mcp.tools."):
                order.append(f"import:{name.rsplit('.', 1)[-1]}")
            return real_import(name, *args, **kwargs)

        tools_pkg.importlib.import_module = recording_import
        try:
            tools_pkg.discover_and_register_tools()
        finally:
            tools_pkg.importlib.import_module = real_import
    finally:
        tools_pkg.rebuild_recursive_ast_cycle = original

    assert order, "neither the rebuild nor any import was observed"
    assert (
        order[0] == "rebuild"
    ), f"the AST rebuild must run before any tool module import; observed {order[:3]}"


def test_the_mcp_mirrors_have_not_drifted_from_the_service_models():
    """`BatchXItemToolResult(**r.model_dump())` silently DROPS unknown fields.

    pydantic's default is `extra="ignore"`, so a field added to the service model
    and forgotten on the MCP mirror is present over REST and absent over MCP,
    with no exception and a green suite. Item 92/128 added
    `approval_fingerprint`/`approval_reasons` in exactly that shape.
    """
    from querygate.execution import results
    from querygate.mcp.tools import query as query_tools

    pairs = [
        ("BatchQueryItemToolResult", results.BatchQueryItemResult),
        ("BatchExplainItemToolResult", results.BatchExplainItemResult),
        ("BatchVerdictItemToolResult", results.BatchVerdictItemResult),
        ("VerdictPlanToolResult", results.VerdictPlan),
    ]
    for mirror_name, service_model in pairs:
        mirror = getattr(query_tools, mirror_name, None)
        if mirror is None:  # pragma: no cover - mirror renamed or removed
            continue
        missing = set(service_model.model_fields) - set(mirror.model_fields)
        assert not missing, (
            f"{mirror_name} is missing {sorted(missing)} present on "
            f"{service_model.__name__}. `**model_dump()` drops them silently, so the "
            "field would be visible over REST and invisible over MCP."
        )
