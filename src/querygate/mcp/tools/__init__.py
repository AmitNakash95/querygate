"""Registration of MCP tool modules.

**This list is explicit on purpose — do not replace it with a filesystem scan.**
It used to be `Path(__file__).parent.glob("*.py")`, which works only when the
package is unpacked `.py` files on disk. Item 214's compiler spike measured the
consequence: in a Nuitka standalone build there are no `.py` files to glob, so
the scan matched nothing, `discover_and_register_tools()` returned **0**, and
the MCP server advertised **zero tools** — with no exception, no warning, and a
process that started and served happily. The same break applies to any frozen
or zipped deployment (PyInstaller, zipapp, a zipimported wheel).

A static tuple is also what lets a compiler see these modules at all: an
`importlib.import_module(f"...{path.stem}")` call is invisible to static
analysis, so the modules were not even guaranteed to be in the binary.

`tests/unit/test_mcp_tool_registration.py` asserts this tuple matches the
directory listing, so adding a tool file without registering it fails the suite
rather than silently shipping a tool nobody can call.
"""

from __future__ import annotations

import importlib

from querygate.query_ast.models import rebuild_recursive_ast_cycle

#: Every MCP tool module, imported for their registration side effects.
#: CLAUDE.md's testing gotchas note these six deliberately do *not* use
#: `from __future__ import annotations` — `MCPServer` resolves each tool's
#: forward references against the wrapping function's `__globals__`.
TOOL_MODULES: tuple[str, ...] = (
    "connections",
    "help",
    "query",
    "schema",
    "templates",
    "write",
)


def discover_and_register_tools() -> int:
    """Import every registered tool module and return the count imported.

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
    for name in TOOL_MODULES:
        importlib.import_module(f"querygate.mcp.tools.{name}")
    return len(TOOL_MODULES)
