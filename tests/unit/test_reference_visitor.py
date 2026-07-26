"""Contract tests for the canonical AST reference visitor (TODO.md item 96).

`iter_column_refs` is the single authority for "every place a Table.Column
reference can appear in a StructuredQuery" — policy validation (allow/deny +
the item-49 masked-column rule), `referenced_tables`, and schema validation's
table collection all consume it instead of hand-maintaining parallel walks.
These tests pin the position taxonomy and the byte-for-byte set of references
so a future AST field that adds a new reference position is caught here (add
the position to the visitor and this test, in one place) rather than silently
opening a policy/schema hole in one forgotten copy.
"""

from __future__ import annotations

import pytest

from querygate.query_ast.models import (
    AggregateSelectItem,
    ColArg,
    JoinSpec,
    OrderBySpec,
    Predicate,
    ScalarFunctionSelectItem,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)
from querygate.validation.policy_validation import referenced_tables
from querygate.validation.schema_validation import (
    RefPosition,
    iter_column_refs,
    iter_where_predicates,
)

pytestmark = pytest.mark.unit


def _kitchen_sink() -> StructuredQuery:
    """A query that touches every reference position the visitor knows about."""
    return StructuredQuery(
        from_table="orders",
        select=[
            "orders.id",  # SELECT_PROJECTION_BARE
            ScalarFunctionSelectItem(  # SELECT_NESTED
                fn="upper", args=[ColArg(col="orders.status")], alias="st"
            ),
        ],
        joins=[
            JoinSpec(
                table="order_items",
                on=["orders.id", "order_items.order_id"],  # JOIN_ON x2
                extra_on=[["orders.tenant_id", "order_items.tenant_id"]],  # JOIN_EXTRA_ON x2
            )
        ],
        where=WhereGroup(
            and_terms=[
                Predicate(col="orders.total", op="gt", value=0),  # WHERE
                WhereGroup(not_terms=Predicate(col="order_items.sku", op="eq", value="x")),  # WHERE
            ]
        ),
        group_by=["orders.id"],  # GROUP_BY (dotted)
        having=Predicate(col="orders.total", op="gt", value=1),  # HAVING
        order_by=[OrderBySpec(col="orders.id", dir="asc")],  # ORDER_BY (dotted)
    )


def test_visitor_covers_every_position_with_expected_refs():
    refs = list(iter_column_refs(_kitchen_sink()))
    by_position: dict[RefPosition, list[str]] = {}
    for cr in refs:
        by_position.setdefault(cr.position, []).append(cr.ref)

    assert by_position[RefPosition.SELECT_PROJECTION_BARE] == ["orders.id"]
    assert by_position[RefPosition.SELECT_NESTED] == ["orders.status"]
    assert by_position[RefPosition.JOIN_ON] == ["orders.id", "order_items.order_id"]
    assert by_position[RefPosition.JOIN_EXTRA_ON] == ["orders.tenant_id", "order_items.tenant_id"]
    assert by_position[RefPosition.WHERE] == ["orders.total", "order_items.sku"]
    assert by_position[RefPosition.GROUP_BY] == ["orders.id"]
    assert by_position[RefPosition.HAVING] == ["orders.total"]
    assert by_position[RefPosition.ORDER_BY] == ["orders.id"]


def test_top_n_positions_only_yield_dotted_refs():
    """group_by/order_by/top_n may reference a bare select alias (no table);
    those are NOT column references and must be filtered out, exactly as the
    superseded walks did."""
    query = StructuredQuery(
        from_table="orders",
        select=[AggregateSelectItem(fn="count", col="*", alias="cnt")],
        group_by=["orders.region"],
        top_n=TopNSpec(
            partition_by=["orders.region"],  # TOP_N_PARTITION (dotted -> yielded)
            order_by=[OrderBySpec(col="cnt", dir="desc")],  # bare alias -> filtered out
            n=3,
        ),
    )
    positions = {cr.position: cr.ref for cr in iter_column_refs(query)}
    assert positions.get(RefPosition.TOP_N_PARTITION) == "orders.region"
    # the bare "cnt" alias in top_n.order_by is not a column reference
    assert RefPosition.TOP_N_ORDER not in positions


def test_only_bare_projection_is_exempt_from_masked_column_rule():
    """The masked-column rule (item 49) rejects a masked column anywhere except
    a bare projection. The visitor's position taxonomy is what encodes that:
    exactly one position is exempt, and a column nested in a select function is
    NOT exempt."""
    query = _kitchen_sink()
    non_projection = [
        cr
        for cr in iter_column_refs(query)
        if cr.position is not RefPosition.SELECT_PROJECTION_BARE
    ]
    # the nested upper(orders.status) is a non-projection ref (subject to masking)
    assert "orders.status" in {cr.ref for cr in non_projection}
    # the bare orders.id projection is the sole exempt occurrence of that position
    bare = [
        cr for cr in iter_column_refs(query) if cr.position is RefPosition.SELECT_PROJECTION_BARE
    ]
    assert [cr.ref for cr in bare] == ["orders.id"]


def test_referenced_tables_maps_alias_to_physical():
    """A self-join via alias: table-level policy must see the physical table,
    never the alias — referenced_tables consumes the visitor and maps back."""
    query = StructuredQuery(
        from_table="employees",
        from_alias="e",
        select=["e.id", "m.id"],
        joins=[
            JoinSpec(
                table="employees",
                alias="m",
                on=["e.manager_id", "m.id"],
            )
        ],
    )
    assert referenced_tables(query) == {"employees"}


# --------------------------------------------------------------------------- #
# iter_where_predicates — the single canonical WHERE-predicate walk (item 111). #
# The read policy/schema + write policy/schema validators all consume it, so    #
# its tree traversal is pinned here in one place (the item-96 discipline, for    #
# the predicate axis rather than the column-ref axis).                          #
# --------------------------------------------------------------------------- #


def test_iter_where_predicates_yields_every_leaf_through_and_or_not():
    """A nested boolean tree mixing and/or/not: every Predicate leaf is yielded
    in document order, and no WhereGroup node is yielded."""
    tree = WhereGroup(
        and_terms=[
            Predicate(col="orders.id", op="gt", value=0),
            WhereGroup(
                or_terms=[
                    Predicate(col="orders.status", op="eq", value="new"),
                    WhereGroup(not_terms=Predicate(col="orders.total", op="lt", value=10)),
                ]
            ),
        ]
    )
    preds = list(iter_where_predicates(tree))
    assert all(isinstance(p, Predicate) for p in preds)
    assert [(p.col, p.value) for p in preds] == [
        ("orders.id", 0),
        ("orders.status", "new"),
        ("orders.total", 10),
    ]


def test_iter_where_predicates_on_a_bare_predicate():
    """A WHERE that is a single Predicate (not a group) yields just that node."""
    leaf = Predicate(col="orders.id", op="eq", value=1)
    assert list(iter_where_predicates(leaf)) == [leaf]


# --------------------------------------------------------------------------- #
# Item 100 — the bounded scalar Expression substrate
#
# The visitor is the enforcement chokepoint: an Expression column ref that is
# not yielded here bypasses column allow/deny AND the item-49 masked-column rule
# entirely. These pin that every union member contributes its refs, at any depth
# and in every position an expression can occupy.
# --------------------------------------------------------------------------- #
def _every_expression_member() -> dict:
    """One expression using EVERY member of the closed union, with a distinct
    column at each position, so a missed member shows up as a missing ref."""
    return {
        "op": "+",  # BinaryOpExpr
        "left": {
            "fn": "coalesce",  # FunctionExpr (nested args)
            "args": [
                {"cast": {"col": "t.cast_col"}, "to": "numeric"},  # CastExpr
                {"literal": 0},  # LiteralExpr — contributes no ref
            ],
        },
        "right": {
            "when": [  # CaseExpr
                {
                    # A CASE condition is a WhereNode, NOT an Expression: its
                    # refs are ordinary Predicate refs, so the ref walk has to
                    # cross out of the union and back.
                    "when": {
                        "col": "t.cond_col",
                        "op": "gt",
                        "value_col": "t.cond_value_col",
                    },
                    "then": {"col": "t.then_col"},  # ColumnExpr
                }
            ],
            "else": {
                # A predicate nested inside a CASE condition may itself carry a
                # whole expression — the deepest reachable position.
                "op": "*",
                "left": {"col": "t.else_col"},
                "right": {"literal": 2},
            },
        },
    }


_EXPRESSION_MEMBER_REFS = {
    "t.cast_col",
    "t.cond_col",
    "t.cond_value_col",
    "t.then_col",
    "t.else_col",
}


def test_expression_refs_are_yielded_from_every_union_member():
    from querygate.validation.schema_validation import expression_column_refs

    refs = set(expression_column_refs(_to_expression_model(_every_expression_member())))
    assert refs == _EXPRESSION_MEMBER_REFS


def _to_expression_model(payload: dict):
    """Parse a raw expression payload through the real union."""
    from querygate.query_ast.models import ExpressionSelectItem

    return ExpressionSelectItem.model_validate({"expr": payload, "as": "x"}).expr


def test_expression_refs_in_a_projection_are_select_nested_not_bare():
    """SELECT_PROJECTION_BARE is the ONLY position a masked column may appear.
    A column inside a computed projection must NOT claim that position, or
    masking silently stops applying to arithmetic."""
    query = StructuredQuery.model_validate(
        {
            "from": "t",
            "select": [{"expr": _every_expression_member(), "as": "computed"}],
        }
    )
    refs = list(iter_column_refs(query))
    assert {r.ref for r in refs} == _EXPRESSION_MEMBER_REFS
    assert {r.position for r in refs} == {RefPosition.SELECT_NESTED}


def test_aggregate_expression_argument_refs_are_visited():
    query = StructuredQuery.model_validate(
        {
            "from": "t",
            "select": [{"fn": "sum", "arg": _every_expression_member(), "as": "total"}],
        }
    )
    assert {r.ref for r in iter_column_refs(query)} == _EXPRESSION_MEMBER_REFS


def test_predicate_expression_refs_are_visited_on_both_sides():
    query = StructuredQuery.model_validate(
        {
            "from": "t",
            "select": ["t.id"],
            "where": {
                "expr": {"op": "*", "left": {"col": "t.qty"}, "right": {"col": "t.price"}},
                "op": "gt",
                "value_expr": {"op": "+", "left": {"col": "t.floor"}, "right": {"literal": 1}},
            },
        }
    )
    where_refs = {r.ref for r in iter_column_refs(query) if r.position is RefPosition.WHERE}
    assert where_refs == {"t.qty", "t.price", "t.floor"}


def test_case_select_item_and_equivalent_expression_yield_the_same_refs():
    """CaseSelectItem is the projection SPELLING of CaseExpr — if the two walks
    ever diverge, one of them is a bypass."""
    branch = {
        "when": [{"when": {"col": "t.a", "op": "eq", "value": 1}, "then": {"col": "t.b"}}],
        "else": {"col": "t.c"},
    }
    as_case_item = StructuredQuery.model_validate({"from": "t", "select": [{**branch, "as": "k"}]})
    as_expression = StructuredQuery.model_validate(
        {"from": "t", "select": [{"expr": branch, "as": "k"}]}
    )
    assert {r.ref for r in iter_column_refs(as_case_item)} == {
        r.ref for r in iter_column_refs(as_expression)
    }


def test_referenced_tables_sees_tables_reachable_only_through_an_expression():
    """Table-level allow/deny reads from `referenced_tables`; a table named ONLY
    deep inside an expression must still be checked."""
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "joins": [{"table": "items", "on": ["orders.id", "items.order_id"]}],
            "select": [
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "items.qty"},
                        "right": {"col": "items.price"},
                    },
                    "as": "revenue",
                }
            ],
        }
    )
    assert referenced_tables(query) == {"orders", "items"}


# --------------------------------------------------------------------------- #
# Item 100 — exhaustiveness of the closed Expression union.
#
# `iter_expression_parts` is the ONE recursion over the union; the ref walk, the
# node/depth caps, and the CASE-condition rules are all filters over it. A new
# member that is added to the union but not to the walk is a silent policy and
# cap bypass, so these tests are parameterized over `typing.get_args(Expression)`
# — adding a member without teaching the walk fails here by construction rather
# than by someone remembering to write a test.
# --------------------------------------------------------------------------- #
_MARKER = "t.marker_col"

# One instance of each union member, each wrapping the marker column so the walk
# has to descend into that member's children to find it.
_MEMBER_PAYLOADS = {
    "ColumnExpr": {"col": _MARKER},
    "LiteralExpr": None,  # a literal carries no ref by construction — see below
    "BinaryOpExpr": {"op": "+", "left": {"col": _MARKER}, "right": {"literal": 1}},
    "FunctionExpr": {"fn": "lower", "args": [{"col": _MARKER}]},
    "CastExpr": {"cast": {"col": _MARKER}, "to": "text"},
    "CaseExpr": {
        "when": [{"when": {"col": _MARKER, "op": "eq", "value": 1}, "then": {"literal": 1}}]
    },
}


def test_every_expression_union_member_has_a_payload_under_test():
    """Guard on the guard: if a member joins the union without a payload here,
    the parameterized tests below would silently stop covering it."""
    import typing

    from querygate.query_ast.models import Expression

    assert {member.__name__ for member in typing.get_args(Expression)} == set(_MEMBER_PAYLOADS)


# ColumnExpr IS the marker (a leaf), so it has no children to descend into;
# LiteralExpr carries no ref at all. Both properties are pinned separately.
_LEAF_MEMBERS = {"ColumnExpr", "LiteralExpr"}
_REF_BEARING_MEMBERS = [name for name, p in _MEMBER_PAYLOADS.items() if p is not None]
_COMPOSITE_MEMBERS = [name for name in _REF_BEARING_MEMBERS if name not in _LEAF_MEMBERS]


@pytest.mark.parametrize("member", _REF_BEARING_MEMBERS)
def test_walk_finds_the_column_inside_every_union_member(member):
    """Every member's columns are reached by the one canonical walk, so they are
    policy-checked and mask-checked wherever the member is nested."""
    from querygate.validation.schema_validation import expression_column_refs

    expr = _to_expression_model(_MEMBER_PAYLOADS[member])
    assert _MARKER in set(expression_column_refs(expr)), f"{member} hides its column refs"


@pytest.mark.parametrize("member", _COMPOSITE_MEMBERS)
def test_walk_descends_into_every_composite_union_member(member):
    """A composite member must yield more than itself, or its size is not
    counted toward `max_expression_nodes` and its depth is under-reported."""
    from querygate.validation.schema_validation import iter_expression_parts

    expr = _to_expression_model(_MEMBER_PAYLOADS[member])
    assert len(list(iter_expression_parts(expr))) > 1, f"{member} is not descended into"


def test_literal_is_the_only_member_that_contributes_no_refs():
    """Pinned explicitly so `LiteralExpr` being excluded above reads as a
    deliberate property, not an oversight in the parameterization."""
    from querygate.validation.schema_validation import expression_column_refs

    assert list(expression_column_refs(_to_expression_model({"literal": "x"}))) == []


def test_walk_fails_closed_on_an_unknown_expression_node():
    """A future union member that reaches the walk without a branch must RAISE,
    not be yielded childless — silently contributing neither refs (a policy and
    masking bypass) nor size (a cap bypass) is the failure mode that matters."""
    from querygate.core.exceptions import QueryValidationError
    from querygate.validation.schema_validation import iter_expression_parts

    class NotAnExpression:
        pass

    with pytest.raises(QueryValidationError, match="Unsupported expression node"):
        list(iter_expression_parts(NotAnExpression()))


@pytest.mark.parametrize("member", _REF_BEARING_MEMBERS)
def test_compiler_handles_every_union_member(member):
    """The compiler is the one recursion over the union that cannot be folded
    into the walk (it produces SQL, not a traversal). Pin that it too covers
    every member — and it likewise raises rather than silently mis-rendering."""
    import sqlalchemy as sa

    from querygate.compiler.sqlalchemy_compiler import _compile_expression

    metadata = sa.MetaData()
    table = sa.Table("t", metadata, sa.Column("marker_col", sa.Integer))
    compiled = _compile_expression(
        _to_expression_model(_MEMBER_PAYLOADS[member]), {"t": table}, "postgresql"
    )
    assert compiled is not None


def test_compiler_fails_closed_on_an_unknown_expression_node():
    from querygate.compiler.sqlalchemy_compiler import _compile_expression
    from querygate.core.exceptions import QueryValidationError

    class NotAnExpression:
        pass

    with pytest.raises(QueryValidationError, match="Unsupported expression node"):
        _compile_expression(NotAnExpression(), {}, "postgresql")
