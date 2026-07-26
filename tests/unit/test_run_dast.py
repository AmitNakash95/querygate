"""Unit tests for the DAST gate's schema pruning (scripts/run_dast.py).

These exist because of a real CI failure. Item 100 widened the read AST's
recursion (`Expression` → `CaseExpr` → `CaseWhen` → `WhereNode` → `Predicate` →
`Expression`, over a six-member union) and the DAST job started dying with
SIGKILL/137. The AST endpoints were already excluded — but with
`--exclude-path-regex`, which skips *executing* an operation while still parsing
its schema, and Schemathesis canonicalises every component in the document
before running anything. So even `GET /help/search` OOM-killed the scanner.

The fix removes those paths from the document and garbage-collects the
now-unreachable components. The tests below pin the two properties that make
that safe, and — importantly — they run in the fast unit suite: the failure they
guard against otherwise shows up only as a three-minute out-of-memory kill in
CI, with a message that looks like a security finding rather than a crash.
"""

from __future__ import annotations

import pytest

from scripts import run_dast

pytestmark = pytest.mark.unit

_SCHEMA_REF_PREFIX = "#/components/schemas/"


def _live_spec() -> dict:
    """The real OpenAPI document, built the same way the app serves it."""
    from querygate.api.app import app

    return app.openapi()


def _refs(node: object) -> set:
    found: set = set()
    run_dast._schema_refs(node, found)
    return found


def test_pruning_removes_the_recursive_ast_components():
    """The recursive read/write AST definitions must not reach the scanner —
    they are what blows its memory, and no in-scope operation references them."""
    pruned = run_dast.prune_schema(_live_spec())
    remaining = set(pruned["components"]["schemas"])

    must_be_gone = {
        "StructuredQuery",
        "Predicate",
        "WhereGroup",
        "CaseWhen",
        "BinaryOpExpr",
        "FunctionExpr",
        "CaseExpr",
        "CastExpr",
        "ColumnExpr",
        "LiteralExpr",
        "ExpressionSelectItem",
        "WindowSelectItem",
        "WindowSpec",
        "WindowFrame",
        "WindowBound",
        # item 114's write filter types — self-recursive like WhereGroup.
        "WritePredicate",
        "WriteWhereGroup",
    }
    assert not (remaining & must_be_gone), sorted(remaining & must_be_gone)


def test_pruned_schema_has_no_dangling_references():
    """Garbage collection must never remove something still referenced —
    otherwise the scanner fails on an unresolvable $ref instead of the OOM, which
    is a different broken, not a fixed."""
    pruned = run_dast.prune_schema(_live_spec())
    components = pruned["components"]["schemas"]

    referenced = _refs(pruned["paths"])
    for schema in components.values():
        referenced |= _refs(schema)

    dangling = sorted(name for name in referenced if name not in components)
    assert not dangling, f"pruned schema references missing components: {dangling}"


def test_pruned_component_graph_is_acyclic():
    """THE regression guard for the CI failure.

    A `$ref` cycle among the components handed to Schemathesis is what triggers
    the canonicalisation blow-up. Asserting acyclicity catches a *new* recursive
    model reaching the scanner — e.g. someone adding a nested request body to an
    in-scope endpoint — in the unit suite, instead of as an OOM kill in CI.

    If this fails: either exclude the new operation in `EXCLUDED_PATHS` (and say
    where it is covered instead — the AST surface is covered by
    tests/security/test_malformed_input_fuzzing.py and test_write_boundary.py),
    or flatten the model so it is not self-referential.
    """
    pruned = run_dast.prune_schema(_live_spec())
    components = pruned["components"]["schemas"]
    edges = {name: _refs(schema) & set(components) for name, schema in components.items()}

    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(components, WHITE)
    cycles: list[list[str]] = []

    def visit(node: str, stack: list) -> None:
        colour[node] = GREY
        stack.append(node)
        for nxt in sorted(edges[node]):
            if colour[nxt] is GREY:
                cycles.append(stack[stack.index(nxt) :] + [nxt])
            elif colour[nxt] is WHITE:
                visit(nxt, stack)
        stack.pop()
        colour[node] = BLACK

    for name in sorted(components):
        if colour[name] is WHITE:
            visit(name, [])

    assert not cycles, "recursive $ref cycle would OOM the DAST scanner: " + "; ".join(
        " -> ".join(cycle) for cycle in cycles[:3]
    )


def test_excluded_paths_match_the_ast_accepting_operations():
    """Pin the exclusion list against the live schema so a renamed or new AST
    route is noticed here rather than by an OOM."""
    spec = _live_spec()
    excluded = {path for path in spec["paths"] if run_dast.EXCLUDED_PATHS.search(path)}
    assert excluded == {
        "/api/v1/admin/config/simulate",
        "/api/v1/{connection}/query",
        "/api/v1/{connection}/query/approve",
        "/api/v1/{connection}/query/batch",
        "/api/v1/{connection}/query/explain",
        "/api/v1/query-templates/{template_id}/run",
        "/api/v1/{connection}/write/approve",
        "/api/v1/{connection}/write/execute",
        "/api/v1/{connection}/write/preview",
    }


def test_pruning_keeps_the_bulk_of_the_surface_in_scope():
    """Guard against the pruning quietly gutting coverage: the DAST gate is only
    meaningful if it still fuzzes the non-AST surface."""
    spec = _live_spec()
    pruned = run_dast.prune_schema(spec)
    assert len(pruned["paths"]) >= len(spec["paths"]) - 10
    assert len(pruned["paths"]) > 50
