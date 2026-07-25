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
