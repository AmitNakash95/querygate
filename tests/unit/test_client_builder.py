"""Tests for the typed client-side query builder (TODO.md item 51, phase 1).

Two things are proven here:

1. **Fidelity** — the builder emits the exact wire JSON a hand-written
   ``StructuredQuery`` body would, and everything it emits round-trips back
   through the real server model unchanged. The builder reuses the server's
   own Pydantic models, so an illegal shape raises in ``build()`` with the
   same error the server would return.
2. **Drift guards** — introspective tests that fail if the ``StructuredQuery``
   AST grows a field, a ``SelectItem`` variant, or a ``CompareOp`` the builder
   can't express. This is the "kept in sync via a schema test" acceptance
   criterion from item 51: the builder can never silently fall behind the AST.
"""

from __future__ import annotations

import typing

import pydantic
import pytest

from querygate.client import (
    Query,
    agg,
    and_,
    array_agg,
    asc,
    case,
    case_expr,
    cast,
    col,
    col_fn,
    date_add,
    date_bucket,
    desc,
    expr_fn,
    expr_select,
    extract,
    fn,
    fn_select,
    frame,
    lit,
    not_,
    now,
    or_,
    percentile_cont,
    string_agg,
    when,
    window,
)
from querygate.client.builder import _to_expression
from querygate.query_ast import models as m

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fidelity
# --------------------------------------------------------------------------- #
def test_builder_matches_handwritten_wire_dict():
    """A realistic aggregate query serializes to exactly the JSON the REST docs
    show a caller writing by hand."""
    built = (
        Query.from_("orders")
        .join("customers", on=("orders.customer_id", "customers.id"))
        .select("customers.name", agg.sum("orders.total_amount", as_="total_spend"))
        .where(col("customers.country") == "GB")
        .group_by("customers.name")
        .order_by("total_spend", desc=True)
        .limit(5)
        .intent("top GB customers by total spend")
        .to_dict()
    )

    assert built == {
        "from": "orders",
        "select": [
            "customers.name",
            # An aggregate serializes in its CANONICAL form: `col` is accepted
            # on the wire as sugar but is normalized to `arg` by the model's
            # before-validator, so exactly one shape reaches the compiler, the
            # caps, and the reference visitor (item 100, 2026-07-25 Decision Log).
            {"fn": "sum", "arg": {"col": "orders.total_amount"}, "as": "total_spend"},
        ],
        "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
        "where": {"col": "customers.country", "op": "eq", "value": "GB"},
        "group_by": ["customers.name"],
        "order_by": [{"col": "total_spend", "dir": "desc"}],
        "limit": 5,
        "intent": "top GB customers by total spend",
    }


def test_aggregate_col_sugar_and_arg_parse_to_the_identical_model():
    """`col` is kept permanently as sugar for `arg` (2026-07-25 Decision Log).
    It is a SPELLING, not a second structural shape: both wire forms must
    produce byte-identical models, or the "one shape at the enforcement layer"
    property that justified keeping the sugar is lost."""
    sugar = m.AggregateSelectItem.model_validate(
        {"fn": "sum", "col": "orders.total_amount", "as": "t"}
    )
    canonical = m.AggregateSelectItem.model_validate(
        {"fn": "sum", "arg": {"col": "orders.total_amount"}, "as": "t"}
    )
    assert sugar == canonical
    assert sugar.col is None and isinstance(sugar.arg, m.ColumnExpr)

    with pytest.raises(pydantic.ValidationError, match="exactly one of 'col' or 'arg'"):
        m.AggregateSelectItem.model_validate(
            {"fn": "sum", "col": "orders.total_amount", "arg": {"col": "orders.total_amount"}}
        )


def test_output_roundtrips_through_the_real_model():
    """Whatever the builder emits validates cleanly as a StructuredQuery and is
    identical to the model the builder itself constructed."""
    q = (
        Query.from_("order_items")
        .select("order_items.id", agg.count("*", as_="n"))
        .where(col("order_items.quantity").between(1, 10))
        .group_by("order_items.id")
    )
    reparsed = m.StructuredQuery.model_validate(q.to_dict())
    assert reparsed == q.build()


def test_multiple_where_calls_are_anded():
    q = (
        Query.from_("orders")
        .select("orders.id")
        .where(col("orders.status") == "completed")
        .where(col("orders.total_amount") > 100)
    )
    assert q.to_dict()["where"] == {
        "and": [
            {"col": "orders.status", "op": "eq", "value": "completed"},
            {"col": "orders.total_amount", "op": "gt", "value": 100},
        ]
    }


def test_single_where_is_not_wrapped_in_a_group():
    q = Query.from_("orders").select("orders.id").where(col("orders.id") == 1)
    assert q.to_dict()["where"] == {"col": "orders.id", "op": "eq", "value": 1}


def test_column_to_column_comparison_uses_value_col():
    pred = col("order_items.unit_price") > col("order_items.quantity")
    assert pred.model_dump(exclude_none=True) == {
        "col": "order_items.unit_price",
        "op": "gt",
        "value_col": "order_items.quantity",
    }


def test_boolean_groups_and_not():
    q = (
        Query.from_("orders")
        .select("orders.id")
        .where(
            and_(
                or_(col("orders.status") == "completed", col("orders.status") == "shipped"),
                not_(col("orders.total_amount").is_null()),
            )
        )
    )
    assert q.to_dict()["where"] == {
        "and": [
            {
                "or": [
                    {"col": "orders.status", "op": "eq", "value": "completed"},
                    {"col": "orders.status", "op": "eq", "value": "shipped"},
                ]
            },
            {"not": {"col": "orders.total_amount", "op": "is_null"}},
        ]
    }


def test_scalar_fn_predicate_and_projection():
    q = (
        Query.from_("customers")
        .select(fn_select("upper", col("customers.name"), as_="shout"))
        .where(fn("lower", col("customers.name")) == "ada")
    )
    body = q.to_dict()
    assert body["select"] == [{"fn": "upper", "args": [{"col": "customers.name"}], "as": "shout"}]
    assert body["where"] == {
        "col_fn": {"fn": "lower", "args": [{"col": "customers.name"}]},
        "op": "eq",
        "value": "ada",
    }


def test_case_with_else_and_literal_and_column_branches():
    item = case(
        when(col("orders.total_amount") > 100, lit("big")),
        when(col("orders.status") == "pending", col("orders.status")),
        else_=lit("other"),
        as_="bucket",
    )
    assert item.model_dump(by_alias=True, exclude_none=True) == {
        "when": [
            {
                "when": {"col": "orders.total_amount", "op": "gt", "value": 100},
                "then": {"literal": "big"},
            },
            {
                "when": {"col": "orders.status", "op": "eq", "value": "pending"},
                "then": {"col": "orders.status"},
            },
        ],
        "else": {"literal": "other"},
        "as": "bucket",
    }


def test_searched_having_with_or_group_builds():
    """item 99: `.having()` takes full WhereNodes, so OR-logic over aggregate
    conditions is expressible client-side, matching the AST."""
    q = (
        Query.from_("orders")
        .select("orders.status", agg.sum("orders.total_amount", as_="s"), agg.count("*", as_="n"))
        .group_by("orders.status")
        .having(or_(col("s") > 500, col("n") < 2))
    )
    dumped = q.to_dict()
    assert dumped["having"] == {
        "or": [
            {"col": "s", "op": "gt", "value": 500},
            {"col": "n", "op": "lt", "value": 2},
        ]
    }


def test_multiple_having_calls_are_and_combined():
    """Several `.having()` predicates AND-combine into one WhereGroup, exactly
    like `.where()` — the pre-item-99 AND semantics are preserved."""
    q = (
        Query.from_("orders")
        .select(agg.count("*", as_="n"), agg.sum("orders.total_amount", as_="s"))
        .group_by("orders.status")
        .having(col("n") > 1)
        .having(col("s") > 10)
    )
    assert q.to_dict()["having"] == {
        "and": [
            {"col": "n", "op": "gt", "value": 1},
            {"col": "s", "op": "gt", "value": 10},
        ]
    }


def test_searched_case_condition_is_a_boolean_group():
    """A CASE `when` accepts a full WhereNode — a searched CASE (item 99)."""
    item = case(
        when(
            and_(col("orders.status") == "completed", col("orders.total_amount") > 100), lit("hot")
        ),
        else_=lit("cold"),
        as_="bucket",
    )
    assert item.model_dump(by_alias=True, exclude_none=True)["when"][0]["when"] == {
        "and": [
            {"col": "orders.status", "op": "eq", "value": "completed"},
            {"col": "orders.total_amount", "op": "gt", "value": 100},
        ]
    }


def test_top_n_with_partition_and_ordering_helpers():
    q = (
        Query.from_("order_items")
        .select("order_items.id")
        .top_n(
            2,
            order_by=[desc("order_items.unit_price"), asc("order_items.id")],
            partition_by=["order_items.order_id"],
            fn="rank",
        )
    )
    assert q.to_dict()["top_n"] == {
        "partition_by": ["order_items.order_id"],
        "order_by": [
            {"col": "order_items.unit_price", "dir": "desc"},
            {"col": "order_items.id"},
        ],
        "n": 2,
        "fn": "rank",
    }


def test_self_join_needs_alias_and_from_alias_are_expressible():
    q = (
        Query.from_("employees", alias="e")
        .join("employees", on=("e.manager_id", "m.id"), alias="m", type="left")
        .select("e.name", "m.name")
    )
    body = q.to_dict()
    assert body["from"] == "employees"
    assert body["from_alias"] == "e"
    assert body["joins"][0]["alias"] == "m"
    assert body["joins"][0]["type"] == "left"


def test_distinct_and_offset_and_composite_join():
    q = (
        Query.from_("orders")
        .distinct()
        .select("orders.status")
        .join(
            "order_items",
            on=("orders.id", "order_items.order_id"),
            extra_on=[("orders.customer_id", "order_items.id")],
        )
        .offset(5)
        .limit(10)
    )
    body = q.to_dict()
    assert body["distinct"] is True
    assert body["offset"] == 5
    assert body["joins"][0]["extra_on"] == [["orders.customer_id", "order_items.id"]]


# --------------------------------------------------------------------------- #
# Validation is the server's, not re-implemented
# --------------------------------------------------------------------------- #
def test_illegal_shape_raises_the_servers_own_error_at_build_time():
    # distinct count(*) is rejected by AggregateSelectItem's own validator.
    with pytest.raises(pydantic.ValidationError):
        agg.count("*", distinct=True)


def test_between_builds_a_two_element_list_predicate():
    pred = col("orders.total_amount").between(10, 100)
    assert pred.model_dump(exclude_none=True) == {
        "col": "orders.total_amount",
        "op": "between",
        "value": [10, 100],
    }


def test_self_join_missing_alias_raises_the_servers_own_error():
    # employees joined to itself with no alias is rejected by StructuredQuery's
    # own self-join validator — the builder does not pre-empt or hide it.
    with pytest.raises(pydantic.ValidationError):
        Query.from_("employees").select("employees.id").join(
            "employees", on=("employees.manager_id", "employees.id")
        ).build()


def test_scalar_fn_args_must_be_wrapped_explicitly():
    with pytest.raises(TypeError):
        fn("lower", "customers.name")  # a bare string is ambiguous


def test_percentile_fraction_out_of_range_raises():
    with pytest.raises(pydantic.ValidationError):
        percentile_cont("orders.total_amount", 1.5)


def test_predicate_helper_in_select_gives_a_clear_error():
    # fn(...) is a predicate target, not a projection — must point at fn_select.
    with pytest.raises(TypeError, match="fn_select"):
        Query.from_("customers").select(fn("upper", col("customers.name")))
    with pytest.raises(TypeError, match="where"):
        Query.from_("customers").select(col("customers.id") == 1)


# --------------------------------------------------------------------------- #
# Drift guards — the builder cannot silently fall behind the AST
# --------------------------------------------------------------------------- #
def test_every_structuredquery_field_is_settable_by_the_builder():
    """Build a query that sets every StructuredQuery field to a non-default
    value, then assert the serialized keys equal the full set of the model's
    serialization aliases. A new AST field that the builder can't set fails
    here."""
    q = (
        Query.from_("employees", alias="e")
        .distinct()
        .select("e.name", agg.count("*", as_="n"))
        .join("employees", on=("e.manager_id", "m.id"), alias="m")
        .where(col("e.name") == "x")
        .group_by("e.name")
        .having(col("n") > 1)
        .order_by("n", desc=True)
        .limit(10)
        .offset(2)
        .top_n(1, order_by=[desc("n")], partition_by=["e.name"])
        .intent("everything")
    )
    dumped = q.build().model_dump(by_alias=True)  # no exclusions: every key present

    expected_aliases = {
        field.serialization_alias or name for name, field in m.StructuredQuery.model_fields.items()
    }
    assert set(dumped.keys()) == expected_aliases


def test_every_non_string_select_item_type_is_constructible():
    """The set of select-item model classes the builder can produce must equal
    the concrete (non-str) members of the SelectItem union. A new aggregate
    variant added to the AST fails here until a builder helper exists."""
    produced = {
        type(agg.count("*")),
        type(date_bucket("orders.created_at", "month")),
        type(string_agg("customers.email", ", ")),
        type(array_agg("order_items.product_name")),
        type(percentile_cont("orders.total_amount", 0.5)),
        type(fn_select("upper", col("customers.name"))),
        type(case(when(col("orders.id") == 1, lit("x")), as_="k")),
        type(expr_select(col("order_items.quantity") * col("order_items.price"), as_="line")),
        type(
            window(
                "sum",
                col("orders.total_amount"),
                as_="running_total",
                order_by=[asc("orders.created_at")],
                frame=frame("rows", None, 0),
            )
        ),
    }
    union_members = {arg for arg in typing.get_args(m.SelectItem) if arg is not str}
    assert produced == union_members


def test_every_expression_union_member_is_constructible():
    """The same drift guard, one level down: a new `Expression` member added to
    the AST fails here until the builder can produce it. Item 100's substrate is
    the dependency of every later engine item, so the SDK must not fall behind
    it silently."""
    produced = {
        type(_to_expression(col("t.c"))),
        type(_to_expression(lit(1))),
        type((col("t.a") * col("t.b")).node),
        type(expr_fn("lower", col("t.c")).node),
        type(cast(col("t.c"), "integer").node),
        type(case_expr(when(col("t.a") == 1, lit(2))).node),
        type(extract("hour", col("t.c")).node),
        type(now().node),
        type(date_add(now(), "day", -7).node),
    }
    assert produced == set(typing.get_args(m.Expression))


def test_date_helpers_wire_their_arguments_to_the_right_fields():
    """Type-only assertions above would pass with `unit` and `amount` swapped,
    since both helpers build the right NODE regardless of argument order."""
    shift = date_add(now("date"), "month", -3).node
    assert (shift.unit, shift.amount) == ("month", -3)
    assert shift.date_add.now == "date"

    part = extract("dayofweek", col("t.c")).node
    assert part.part == "dayofweek"
    assert part.extract.col == "t.c"


def test_every_compare_op_is_reachable_through_the_dsl():
    c = col("t.c")
    reachable = {
        (c == 1).op,
        (c != 1).op,
        (c < 1).op,
        (c <= 1).op,
        (c > 1).op,
        (c >= 1).op,
        c.in_([1, 2]).op,
        c.not_in([1, 2]).op,
        c.like("a%").op,
        c.between(1, 2).op,
        c.is_null().op,
        c.is_not_null().op,
    }
    assert reachable == set(typing.get_args(m.CompareOp))


def test_every_aggregate_fn_is_reachable():
    reachable = {
        agg.count("*").fn,
        agg.sum("t.c").fn,
        agg.avg("t.c").fn,
        agg.min("t.c").fn,
        agg.max("t.c").fn,
        agg.stddev("t.c").fn,
        agg.variance("t.c").fn,
    }
    assert reachable == set(typing.get_args(m.AggregateFn))


def test_every_scalar_fn_is_reachable():
    one_arg = {
        fn("lower", col("t.c")).call.fn,
        fn("upper", col("t.c")).call.fn,
        fn("trim", col("t.c")).call.fn,
    }
    multi_arg = {
        fn("coalesce", col("t.c"), lit("x")).call.fn,
        fn("concat", col("t.c"), lit("x")).call.fn,
    }
    assert one_arg | multi_arg == set(typing.get_args(m.ScalarFn))


def test_col_fn_alias_matches_fn():
    assert col_fn("lower", col("t.c")).call == fn("lower", col("t.c")).call


# --------------------------------------------------------------------------- #
# Item 103 — the general join condition and the two new join types
# --------------------------------------------------------------------------- #
def test_join_with_a_range_condition_builds_the_condition_form():
    """`condition=[...]` AND-combines exactly like `.where()`, and leaves `on` unset."""
    q = (
        Query.from_("products")
        .select("products.name")
        .join(
            "bands",
            condition=[
                col("products.price") >= col("bands.lo"),
                col("products.price") <= col("bands.hi"),
            ],
        )
    )
    join = q.to_dict()["joins"][0]
    # exclude_none drops the unused form entirely, so the wire body carries
    # exactly one condition spelling — which is the AST's own rule.
    assert "on" not in join
    assert join["condition"] == {
        "and": [
            {"col": "products.price", "op": "gte", "value_col": "bands.lo"},
            {"col": "products.price", "op": "lte", "value_col": "bands.hi"},
        ]
    }


def test_join_with_a_single_condition_is_not_wrapped_in_a_group():
    """One node passes through unwrapped — the same `_and_combine` rule `.where()` uses."""
    q = (
        Query.from_("products")
        .select("products.name")
        .join("bands", condition=[col("products.price") >= col("bands.lo")])
    )
    assert q.to_dict()["joins"][0]["condition"]["op"] == "gte"


def test_cross_join_builds_with_neither_condition_form():
    q = Query.from_("products").select("products.name").join("bands", type="cross")
    join = q.to_dict()["joins"][0]
    assert join["type"] == "cross"
    assert "on" not in join and "condition" not in join


def test_join_with_neither_on_nor_condition_raises_the_servers_own_error():
    """`on` became optional for the cross/condition forms, so the builder can now
    express a join with no condition at all — which the server rejects."""
    with pytest.raises(pydantic.ValidationError, match="exactly one of"):
        Query.from_("products").select("products.name").join("bands").build()


def test_join_with_both_on_and_condition_raises_the_servers_own_error():
    with pytest.raises(pydantic.ValidationError, match="exactly one of"):
        (
            Query.from_("products")
            .select("products.name")
            .join(
                "bands",
                on=("products.id", "bands.id"),
                condition=[col("products.price") >= col("bands.lo")],
            )
            .build()
        )


# --------------------------------------------------------------------------- #
# Set operations (item 104)                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "method,op,kwargs",
    [
        ("union", "union", {}),
        ("union", "union", {"all": True}),
        ("intersect", "intersect", {}),
        ("except_", "except", {}),
    ],
)
def test_every_set_operator_is_expressible(method, op, kwargs):
    q = getattr(
        Query.from_("customers").select("customers.id"),
        method,
    )(Query.from_("leads").select("leads.id"), **kwargs)
    spec = q.to_dict()["set_op"]
    assert spec["op"] == op
    assert spec.get("all", False) is kwargs.get("all", False)
    assert spec["arms"] == [{"from": "leads", "select": ["leads.id"]}]


def test_set_operation_order_by_and_limit_belong_to_the_carrying_query():
    """The builder must place them on the combined statement, not on an arm —
    the server rejects an arm that carries either."""
    wire = (
        Query.from_("customers")
        .select("customers.id")
        .union(Query.from_("leads").select("leads.id"))
        .order_by("id", desc=True)
        .limit(5)
        .to_dict()
    )
    assert wire["order_by"] == [{"col": "id", "dir": "desc"}]
    assert wire["limit"] == 5
    assert "order_by" not in wire["set_op"]["arms"][0]


def test_an_arm_shape_the_server_rejects_raises_the_servers_own_error():
    """The builder adds no validation of its own and hides none: an arity
    mismatch surfaces as the same `pydantic.ValidationError` the server would
    return. (It cannot pin *where* in the chain the error is raised — the arity
    rule belongs to the carrier and fires at `.build()` — so this asserts the
    error contract, not the call site.)"""
    with pytest.raises(pydantic.ValidationError, match="same number of columns"):
        (
            Query.from_("customers")
            .select("customers.id")
            .union(Query.from_("leads").select("leads.id", "leads.name"))
            .build()
        )
