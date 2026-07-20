"""Property-based fuzzing of the SQLAlchemy compiler (TODO.md item 36 phase 1,
extended by item 79 to cover the AST surface added in items 68-77).

`test_compiler.py` proves the compiler handles a fixed set of hand-written
AST shapes correctly. Nothing previously proved the compiler is robust
against the *combinatorics* of `StructuredQuery` — random but valid
combinations of select/join/where/order_by/group_by/top_n that hand-written
cases don't happen to construct. This uses Hypothesis to generate many such
combinations and asserts only the crash-freedom/well-formedness property:
every syntactically valid, in-cap `StructuredQuery` compiles to a renderable
`sqlalchemy.Select`, never raises, and always returns a `limit >= 1`.

This is deliberately scoped to what item 36 calls "phase 1": boundary/property
coverage of the compiler and policy caps. Cross-dialect differential testing
(same AST against Postgres and MSSQL) and malformed-input fuzzing at the
REST/MCP JSON boundary are explicitly deferred — see TODO.md item 36.
"""

from __future__ import annotations

from typing import Dict

import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    CaseSelectItem,
    CaseWhen,
    ColArg,
    JoinSpec,
    LiteralArg,
    OrderBySpec,
    Predicate,
    ScalarFunctionCall,
    ScalarFunctionSelectItem,
    StringAggSelectItem,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)

_NULLS = st.one_of(st.none(), st.sampled_from(["first", "last"]))

_SLOW_SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


def _tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
        sa.Column("country", sa.String(2)),
    )
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("status", sa.String(20)),
        sa.Column("total_amount", sa.Numeric(10, 2)),
    )
    return {"customers": customers, "orders": orders}


_ORDER_COLUMNS = ["orders.id", "orders.customer_id", "orders.status", "orders.total_amount"]
_CUSTOMER_COLUMNS = ["customers.id", "customers.name", "customers.country"]

_STRING_VALUES = st.sampled_from(["completed", "pending", "", "O'Brien", "a" * 200, "unicode-é中"])
_NUMERIC_VALUES = st.one_of(
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False, min_value=-1000, max_value=1000),
)

_PREDICATE_SPECS = st.one_of(
    st.tuples(st.just("orders.status"), st.just("eq"), _STRING_VALUES),
    st.tuples(
        st.just("orders.total_amount"),
        st.sampled_from(["gt", "lt", "gte", "lte", "eq"]),
        _NUMERIC_VALUES,
    ),
    st.tuples(
        st.just("orders.id"),
        st.sampled_from(["eq", "gt", "lt"]),
        st.integers(min_value=1, max_value=10_000),
    ),
)


_VALUE_COL_PAIRS = st.sampled_from(
    [("orders.total_amount", "orders.id"), ("orders.id", "orders.customer_id")]
)


@st.composite
def _value_col_predicate(draw):
    """item 71: comparing two columns instead of a column to a literal."""
    col, other = draw(_VALUE_COL_PAIRS)
    op = draw(st.sampled_from(["eq", "neq", "lt", "lte", "gt", "gte"]))
    return Predicate(col=col, op=op, value_col=other)


@st.composite
def _col_fn_predicate(draw):
    """item 77: a whitelisted scalar function as the predicate's LEFT side."""
    fn = draw(st.sampled_from(["lower", "upper"]))
    op = draw(st.sampled_from(["eq", "neq"]))
    value = draw(_STRING_VALUES)
    return Predicate(
        col_fn=ScalarFunctionCall(fn=fn, args=[ColArg(col="orders.status")]), op=op, value=value
    )


@st.composite
def _literal_predicate(draw):
    col, op, value = draw(_PREDICATE_SPECS)
    return Predicate(col=col, op=op, value=value)


@st.composite
def _predicates(draw, max_count=3):
    n = draw(st.integers(min_value=1, max_value=max_count))
    preds = []
    for _ in range(n):
        # Biased toward plain literal predicates (the historically covered
        # shape) with value_col/col_fn mixed in — not a uniform 1/3 split.
        kind = draw(st.sampled_from(["literal", "literal", "literal", "value_col", "col_fn"]))
        if kind == "value_col":
            preds.append(draw(_value_col_predicate()))
        elif kind == "col_fn":
            preds.append(draw(_col_fn_predicate()))
        else:
            preds.append(draw(_literal_predicate()))
    return preds


@st.composite
def _where_clauses(draw):
    preds = draw(_predicates())
    if len(preds) == 1:
        node = draw(st.one_of(st.just(preds[0]), st.just(WhereGroup(and_terms=preds))))
    else:
        boolean = draw(st.sampled_from(["and", "or"]))
        node = WhereGroup(and_terms=preds) if boolean == "and" else WhereGroup(or_terms=preds)
    if draw(st.booleans()):
        node = WhereGroup(not_terms=node)  # item 71: negated group
    return node


@st.composite
def _row_select_queries(draw):
    """Random plain (non-aggregate) row-select shapes: optional join, select
    width, an optional where tree, order_by, and limit.
    """
    include_join = draw(st.booleans())
    order_by_pool = _ORDER_COLUMNS + (_CUSTOMER_COLUMNS if include_join else [])

    select_cols: list = draw(
        st.lists(st.sampled_from(_ORDER_COLUMNS), min_size=1, max_size=4, unique=True)
    )
    if include_join:
        select_cols = select_cols + draw(
            st.lists(st.sampled_from(_CUSTOMER_COLUMNS), min_size=0, max_size=2, unique=True)
        )
    if draw(st.booleans()):  # item 72: a scalar function projection
        select_cols = select_cols + [
            ScalarFunctionSelectItem(fn="lower", args=[ColArg(col="orders.status")], alias="lc")
        ]
    if draw(st.booleans()):  # item 72: a CASE projection
        select_cols = select_cols + [
            CaseSelectItem(
                when=[
                    CaseWhen(
                        when=Predicate(col="orders.status", op="eq", value="completed"),
                        then=LiteralArg(literal="Done"),
                    )
                ],
                else_=LiteralArg(literal="Open"),
                alias="label",
            )
        ]

    extra_on = (
        [["orders.status", "customers.country"]]  # item 76: composite join key
        if include_join and draw(st.booleans())
        else []
    )
    joins = (
        [
            JoinSpec(
                table="customers",
                type=draw(st.sampled_from(["inner", "left"])),
                on=["orders.customer_id", "customers.id"],
                extra_on=extra_on,
            )
        ]
        if include_join
        else []
    )
    where = draw(st.one_of(st.none(), _where_clauses()))
    order_by = draw(
        st.lists(
            st.builds(
                OrderBySpec,
                col=st.sampled_from(order_by_pool),
                dir=st.sampled_from(["asc", "desc"]),
                nulls=_NULLS,
            ),
            max_size=2,
        )
    )
    limit = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=500)))
    return StructuredQuery(
        from_table="orders",
        select=select_cols,
        distinct=draw(st.booleans()),  # item 69
        joins=joins,
        where=where,
        order_by=order_by,
        limit=limit,
    )


@st.composite
def _aggregate_queries(draw):
    """Random GROUP BY + aggregate + HAVING shapes."""
    # item 80: string_agg mixed into the aggregate pool alongside the plain
    # AggregateSelectItem shapes, alias kept "agg_value" so the having/
    # order_by strategies below keep working unchanged either way — same
    # "fold into the existing strategy" approach item 75 used for
    # stddev/variance rather than a new composite strategy.
    if draw(st.booleans()):
        agg_item = StringAggSelectItem(col="orders.status", delimiter=", ", alias="agg_value")
    else:
        # item 75: stddev/variance mixed into the aggregate function pool.
        agg_fn = draw(st.sampled_from(["count", "sum", "avg", "min", "max", "stddev", "variance"]))
        agg_col = "*" if agg_fn == "count" and draw(st.booleans()) else "orders.total_amount"
        # distinct is invalid with count(*) and with stddev/variance on every
        # dialect (see item 75) — never draw it for those rather than relying
        # on a post-hoc filter.
        distinct = (
            draw(st.booleans())
            if agg_col != "*" and agg_fn not in ("stddev", "variance")
            else False
        )
        agg_item = AggregateSelectItem(fn=agg_fn, col=agg_col, alias="agg_value", distinct=distinct)
    select = ["orders.status", agg_item]
    having = draw(
        st.lists(
            st.builds(
                Predicate,
                col=st.just("agg_value"),
                op=st.sampled_from(["gt", "gte", "lt", "eq"]),
                value=st.integers(min_value=0, max_value=1000),
            ),
            max_size=2,
        )
    )
    order_by = draw(
        st.lists(
            st.builds(
                OrderBySpec,
                col=st.just("agg_value"),
                dir=st.sampled_from(["asc", "desc"]),
                nulls=_NULLS,
            ),
            max_size=1,
        )
    )
    return StructuredQuery(
        from_table="orders",
        select=select,
        group_by=["orders.status"],
        having=having,
        order_by=order_by,
        limit=draw(st.integers(min_value=1, max_value=500)),
    )


@st.composite
def _top_n_queries(draw):
    """Random top_n (per-partition ranking) shapes, with or without an
    aggregate/group_by base query underneath.
    """
    is_aggregate = draw(st.booleans())
    partition_by = draw(st.lists(st.just("orders.status"), max_size=1))
    rank_order = st.builds(
        OrderBySpec,
        col=(
            st.just("agg_value")
            if is_aggregate
            else st.sampled_from(["orders.id", "orders.total_amount"])
        ),
        dir=st.sampled_from(["asc", "desc"]),
        nulls=_NULLS,
    )
    top_n = TopNSpec(
        partition_by=partition_by,
        order_by=draw(st.lists(rank_order, min_size=1, max_size=2)),
        n=draw(st.integers(min_value=1, max_value=20)),
        fn=draw(st.sampled_from(["row_number", "rank", "dense_rank"])),
    )
    if is_aggregate:
        select = ["orders.status", AggregateSelectItem(fn="count", col="*", alias="agg_value")]
        group_by = ["orders.status"]
    else:
        select = ["orders.id", "orders.status", "orders.total_amount"]
        group_by = []
    return StructuredQuery(
        from_table="orders", select=select, group_by=group_by, top_n=top_n, limit=50
    )


def _self_join_tables() -> Dict[str, sa.Table]:
    orders = _tables()["orders"]
    return {"e": orders.alias("e"), "m": orders.alias("m")}


@st.composite
def _self_join_queries(draw):
    """item 70: a structurally distinct shape from the other three
    strategies — a self-join via from_alias/JoinSpec.alias, exercising the
    effective-name machinery (every other strategy only ever references one
    occurrence of each table, so aliasing/self-join resolution is otherwise
    entirely untested by the fuzzer).
    """
    where = draw(
        st.one_of(st.none(), st.just(Predicate(col="e.status", op="eq", value="completed")))
    )
    return StructuredQuery(
        from_table="orders",
        from_alias="e",
        select=["e.id", "e.status", "m.id", "m.status"],
        joins=[JoinSpec(table="orders", alias="m", on=["e.customer_id", "m.customer_id"])],
        where=where,
        order_by=[OrderBySpec(col="e.id", dir=draw(st.sampled_from(["asc", "desc"])))],
        limit=draw(st.integers(min_value=1, max_value=100)),
    )


@_SLOW_SETTINGS
@given(query=_row_select_queries())
def test_compiler_never_crashes_on_row_select_shapes(query):
    tables = _tables()
    stmt, limit = compile_structured_query(query, tables, Policy())
    compiled_text = str(stmt.compile())
    assert "orders" in compiled_text.lower()
    assert isinstance(limit, int) and limit >= 1
    # Must also render with literal binds (the audit/explain path's fallback
    # rendering) without raising.
    str(stmt.compile(compile_kwargs={"literal_binds": True}))


@_SLOW_SETTINGS
@given(query=_aggregate_queries())
def test_compiler_never_crashes_on_aggregate_group_by_shapes(query):
    tables = _tables()
    stmt, limit = compile_structured_query(query, tables, Policy())
    compiled_text = str(stmt.compile()).upper()
    assert "GROUP BY" in compiled_text
    assert isinstance(limit, int) and limit >= 1


@_SLOW_SETTINGS
@given(query=_top_n_queries())
def test_compiler_never_crashes_on_top_n_shapes(query):
    tables = _tables()
    stmt, limit = compile_structured_query(query, tables, Policy())
    compiled_text = str(stmt.compile()).upper()
    assert "OVER" in compiled_text
    assert isinstance(limit, int) and limit >= 1


@_SLOW_SETTINGS
@given(query=_self_join_queries())
def test_compiler_never_crashes_on_self_join_shapes(query):
    tables = _self_join_tables()
    stmt, limit = compile_structured_query(query, tables, Policy())
    compiled_text = str(stmt.compile()).upper()
    assert "JOIN" in compiled_text
    assert isinstance(limit, int) and limit >= 1
    str(stmt.compile(compile_kwargs={"literal_binds": True}))


@_SLOW_SETTINGS
@given(query=_self_join_queries())
def test_compiler_respects_mandatory_row_filter_across_self_join_shapes(query):
    """Mirrors `test_compiler_respects_mandatory_row_filter_across_random_shapes`
    below but for self-joins specifically — kept separate because self-join
    queries need a structurally different `tables` dict (keyed by alias, not
    physical name) than the other three strategies share. Proves item 70's
    guarantee holds across random self-join shapes too: a mandatory filter
    applies to EVERY alias of a self-joined table, not just one.
    """
    from querygate.policy.models import MandatoryRowFilter

    tables = _self_join_tables()
    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="orders", column="status", value="__tenant_marker__")
        ]
    )
    stmt, _ = compile_structured_query(query, tables, policy)
    compiled_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert compiled_text.count("__tenant_marker__") == 2


@_SLOW_SETTINGS
@given(query=st.one_of(_row_select_queries(), _aggregate_queries(), _top_n_queries()))
def test_compiler_respects_mandatory_row_filter_across_random_shapes(query):
    """A mandatory row filter on the `from_table` must survive every random
    shape — it is the multi-tenant isolation guarantee (policy/models.py's
    MandatoryRowFilter), so it must never be silently dropped for any AST
    combination the compiler accepts.
    """
    from querygate.policy.models import MandatoryRowFilter

    tables = _tables()
    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="orders", column="status", value="__tenant_marker__")
        ]
    )
    stmt, _ = compile_structured_query(query, tables, policy)
    compiled_text = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "__tenant_marker__" in compiled_text
