"""Auto-discovery of MCP tool modules."""

from __future__ import annotations

import importlib
from pathlib import Path

from querygate.query_ast.models import rebuild_recursive_ast_cycle


def discover_and_register_tools() -> int:
    """Import all tool modules in mcp/tools/ and return the count imported.

    Forces a clean rebuild of the read AST's recursive cycle
    (`query_ast.models.rebuild_recursive_ast_cycle`) before importing any
    tool module. Tool registration builds each tool's JSON parameter schema
    from its argument model (`run_structured_queries`'s `List[StructuredQuery]`,
    `run_structured_writes`'s write-AST union) over the same recursive model
    graph the `io.github.agitmit/structured-query-ast` MCP extension's schema
    generator does (TODO.md item 131) — and `create_app()` "legitimately runs
    more than once in the same process" (see `mcp/server.py`'s
    `_install_scoped_tool_listing` docstring: every test in this suite, any
    production hot-reload/multi-instantiation path), which is exactly the
    condition under which a shared process was observed to occasionally
    misattach a `$ref`'s sibling `description` between two fields that both
    forward-reference `StructuredQuery`. That never affects validation/
    enforcement (an argument model still validates correctly regardless of a
    missing description), but it can silently degrade the description text in
    the real `tools/list` schema served to MCP clients. Rebuilding here,
    before any tool's argument model schema gets built, closes that gap the
    same way the doc schema generator already does for its own consumer.
    """
    rebuild_recursive_ast_cycle(force=True)
    tools_dir = Path(__file__).resolve().parent
    imported = 0
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        importlib.import_module(f"querygate.mcp.tools.{path.stem}")
        imported += 1
    return imported
