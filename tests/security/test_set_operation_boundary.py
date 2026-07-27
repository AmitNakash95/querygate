"""Adversarial boundary tests for set operations (TODO.md item 104).

A set operation is a **new scope container** — the first at the top level — so
the threat it introduces is precise: every guarantee this engine makes is
enforced per scope, and an arm that is not walked as a scope inherits none of
them. So each attack below is "hide it in arm 2 instead of arm 1" against a rule
that already holds for a plain query: a denied table, a denied column, a masked
column outside a projection, a mandatory row filter, the k-anonymity floor, and
every count cap.

The positive execution cases live in
`tests/integration/test_set_operation_end_to_end.py`; the differential ones (the
same AST executed on real Postgres AND real MSSQL) in
`tests/integration/test_cross_dialect_differential.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql

from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import ColumnMask, ColumnMaskKind, MandatoryRowFilter, Policy
from querygate.query_ast.models import Predicate, SetOpSpec, StructuredQuery
from querygate.validation import schema_validation as sv
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.security

_CONN = "demo"


def _union(arm: StructuredQuery, *more: StructuredQuery, **outer) -> StructuredQuery:
    """A two-(or more)-arm UNION whose FIRST arm is a benign `orders.id` read, so
    every test below differs only in what it hid in the LATER arms."""
    return StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        set_op=SetOpSpec(op="union", arms=[arm, *more]),
        **outer,
    )


# --------------------------------------------------------------------------- #
# The core threat: an arm that is not walked as a scope                        #
# --------------------------------------------------------------------------- #


def test_denied_table_in_a_later_arm_is_rejected():
    query = _union(StructuredQuery(from_table="employees", select=["employees.id"]))
    with pytest.raises(PolicyViolationError, match="employees"):
        validate_policy(query, Policy(denied_tables=["employees"]), _CONN)


def test_denied_column_in_a_later_arm_is_rejected():
    query = _union(StructuredQuery(from_table="customers", select=["customers.email"]))
    with pytest.raises(PolicyViolationError, match="customers.email"):
        validate_policy(query, Policy(denied_columns={"customers": ["email"]}), _CONN)


def test_denied_column_hidden_in_a_later_arms_where_is_rejected():
    """The deepest position an arm allows: not projected, only filtered on."""
    query = _union(
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="customers.email", op="eq", value="x@example.com"),
        )
    )
    with pytest.raises(PolicyViolationError, match="customers.email"):
        validate_policy(query, Policy(denied_columns={"customers": ["email"]}), _CONN)


def test_masked_column_in_a_later_arms_filter_is_rejected():
    """A masked column may only surface as a bare projection (item 49). An arm's
    WHERE is not one, so filtering on it there would leak the real value by
    inference — exactly as it would in the outer query."""
    policy = Policy(
        column_masks={"customers": [ColumnMask(column="email", kind=ColumnMaskKind.HASH)]}
    )
    query = _union(
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="customers.email", op="eq", value="x@example.com"),
        )
    )
    with pytest.raises(PolicyViolationError, match="masked by policy"):
        validate_policy(query, policy, _CONN)


def test_a_set_operation_inside_an_in_subquery_is_still_fully_scoped():
    """Set operations and subqueries compose, so the arms of a set operation
    nested inside an `IN (subquery)` must still be walked. Both containers are
    handled by the one `iter_query_scopes` walk, and this is the test that would
    fail if either stopped descending into the other."""
    nested = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        set_op=SetOpSpec(
            op="union",
            arms=[StructuredQuery(from_table="employees", select=["employees.id"])],
        ),
    )
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.customer_id", op="in", value_subquery=nested),
    )
    with pytest.raises(PolicyViolationError, match="employees"):
        validate_policy(query, Policy(denied_tables=["employees"], max_set_op_arms=5), _CONN)


# --------------------------------------------------------------------------- #
# Arms share one budget — they do not each get their own                       #
# --------------------------------------------------------------------------- #


def test_joins_are_summed_across_arms():
    """3 joins per arm is within `max_joins=5` for each arm alone; 6 across the
    statement is not. Summing is what stops an arm being a cap multiplier."""

    def _arm(prefix: str, table: str) -> StructuredQuery:
        return StructuredQuery(
            from_table=table,
            select=[f"{table}.id"],
            joins=[
                {"table": f"{prefix}{i}", "on": [f"{table}.id", f"{prefix}{i}.id"]}
                for i in range(3)
            ],
        )

    query = _arm("o", "orders")
    query = query.model_copy(
        update={"set_op": SetOpSpec(op="union", arms=[_arm("c", "customers")])}
    )
    with pytest.raises(PolicyViolationError, match="joins exceeds"):
        validate_policy(query, Policy(max_joins=5), _CONN)


def test_where_predicates_are_summed_across_arms():
    def _arm(table: str, n: int) -> StructuredQuery:
        return StructuredQuery(
            from_table=table,
            select=[f"{table}.id"],
            where={"or": [{"col": f"{table}.id", "op": "eq", "value": i} for i in range(n)]},
        )

    query = _arm("orders", 3).model_copy(
        update={"set_op": SetOpSpec(op="union", arms=[_arm("customers", 3)])}
    )
    with pytest.raises(PolicyViolationError, match="where predicate count 6"):
        validate_policy(query, Policy(max_where_predicates=5), _CONN)


def test_select_columns_are_summed_across_arms():
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id", "orders.status"],
        set_op=SetOpSpec(
            op="union",
            arms=[
                StructuredQuery(from_table="customers", select=["customers.id", "customers.name"])
            ],
        ),
    )
    with pytest.raises(PolicyViolationError, match="select exceeds"):
        validate_policy(query, Policy(max_select_columns=3), _CONN)


def test_expression_nodes_are_summed_across_arms():
    arm_expr = {"op": "+", "left": {"col": "customers.id"}, "right": {"literal": 1}}
    query = StructuredQuery(
        from_table="orders",
        select=[
            {"expr": {"op": "+", "left": {"col": "orders.id"}, "right": {"literal": 1}}, "as": "x"}
        ],
        set_op=SetOpSpec(
            op="union",
            arms=[StructuredQuery(from_table="customers", select=[{"expr": arm_expr, "as": "x"}])],
        ),
    )
    # 3 nodes per arm, 6 across the statement.
    with pytest.raises(PolicyViolationError, match="expression node count 6"):
        validate_policy(query, Policy(max_expression_nodes=5), _CONN)


# --------------------------------------------------------------------------- #
# The arm-count cap itself                                                     #
# --------------------------------------------------------------------------- #


def test_arm_count_cap_counts_the_carrying_query_as_arm_one():
    query = _union(
        StructuredQuery(from_table="customers", select=["customers.id"]),
        StructuredQuery(from_table="products", select=["products.id"]),
    )
    validate_policy(query, Policy(max_set_op_arms=3), _CONN)  # exactly at the cap
    with pytest.raises(PolicyViolationError, match="combines 3 arms"):
        validate_policy(query, Policy(max_set_op_arms=2), _CONN)


def test_arm_count_is_summed_across_a_nested_set_operation():
    """Two 2-arm set operations — one at the top level, one inside an
    `IN (subquery)` — are 4 arms against one budget, not 2 against each."""
    nested = StructuredQuery(
        from_table="customers",
        select=["customers.id"],
        set_op=SetOpSpec(
            op="union", arms=[StructuredQuery(from_table="products", select=["products.id"])]
        ),
    )
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.customer_id", op="in", value_subquery=nested),
        set_op=SetOpSpec(
            op="union", arms=[StructuredQuery(from_table="employees", select=["employees.id"])]
        ),
    )
    with pytest.raises(PolicyViolationError, match="combines 4 arms"):
        validate_policy(query, Policy(max_set_op_arms=3), _CONN)


def test_zero_arms_policy_disables_set_operations_entirely():
    query = _union(StructuredQuery(from_table="customers", select=["customers.id"]))
    with pytest.raises(PolicyViolationError, match="set operations are disabled"):
        validate_policy(query, Policy(max_set_op_arms=0), _CONN)
    # A plain query is untouched by the same policy — the cap gates set
    # operations, not every read.
    validate_policy(
        StructuredQuery(from_table="orders", select=["orders.id"]),
        Policy(max_set_op_arms=0),
        _CONN,
    )


# --------------------------------------------------------------------------- #
# Compiled-statement guarantees: filters and the k-anon floor reach every arm  #
# --------------------------------------------------------------------------- #


def _reflected() -> dict:
    metadata = sa.MetaData()
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant", sa.String),
        sa.Column("status", sa.String),
    )
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant", sa.String),
        sa.Column("country", sa.String),
    )
    return {"orders": orders, "customers": customers}


def _compile(query: StructuredQuery, policy: Policy) -> str:
    tables = _reflected()
    arm_tables = {
        id(arm): {name: tables[name] for name in ({arm.from_table} | {j.table for j in arm.joins})}
        for arm in sv.iter_set_op_arms(query)
    }
    stmt, _limit = compile_structured_query(
        query,
        arm_tables[id(query)],
        policy,
        dialect="postgresql",
        subquery_tables=arm_tables,
    )
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_a_mandatory_row_filter_is_applied_to_every_arm():
    """The plan's headline rule for this item: a set operation must not be a
    channel to dodge a per-table filter. Both arms read a filtered table, so both
    halves of the compiled statement must carry the filter."""
    policy = Policy(
        mandatory_row_filters=[
            MandatoryRowFilter(table="orders", column="tenant", value="acme"),
            MandatoryRowFilter(table="customers", column="tenant", value="acme"),
        ]
    )
    sql = _compile(_union(StructuredQuery(from_table="customers", select=["customers.id"])), policy)
    assert sql.count("tenant = 'acme'") == 2, sql


def test_the_k_anonymity_floor_is_applied_to_every_aggregating_arm():
    """`min_group_size` compiles to a HAVING on each aggregate SELECT. An arm
    without it would return below-floor groups through the same response."""
    query = StructuredQuery(
        from_table="orders",
        select=["orders.status", {"fn": "count", "col": "*", "as": "n"}],
        group_by=["orders.status"],
        set_op=SetOpSpec(
            op="union",
            arms=[
                StructuredQuery(
                    from_table="customers",
                    select=["customers.country", {"fn": "count", "col": "*", "as": "n"}],
                    group_by=["customers.country"],
                )
            ],
        ),
    )
    sql = _compile(query, Policy(min_group_size=5))
    assert sql.count("count(*) >= 5") == 2, sql


def test_the_compound_limit_survives_on_every_dialect():
    """`clamp_limit` is a guardrail, and SQLAlchemy's MSSQL dialect silently DROPS
    `.limit()` applied to a CompoundSelect — measured, which is why the compiler
    wraps the compound in a derived table first. Without the wrap this query
    returns unbounded rows on MSSQL only."""
    query = _union(StructuredQuery(from_table="customers", select=["customers.id"]), limit=7)
    tables = _reflected()
    arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
    for dialect, engine, marker in (
        ("postgresql", postgresql.dialect(), "LIMIT 7"),
        ("mssql", mssql.dialect(), "TOP 7"),
    ):
        stmt, limit = compile_structured_query(
            query, tables, Policy(), dialect=dialect, subquery_tables=arm_tables
        )
        rendered = str(stmt.compile(dialect=engine, compile_kwargs={"literal_binds": True}))
        assert limit == 7
        assert marker in rendered, rendered


def test_the_row_limit_ceiling_applies_unless_every_arm_aggregates():
    """`max_limit_aggregate` is deliberately far higher than `max_limit` because
    an aggregated response is summarised rows. A set operation gets that higher
    ceiling only when EVERY arm aggregates — one raw-row arm means the response
    contains raw rows, and `any()` here would hand a raw read the aggregate
    ceiling (1000 rows by default instead of 100)."""
    aggregate_arm = StructuredQuery(
        from_table="customers",
        select=["customers.country", {"fn": "count", "col": "*", "as": "n"}],
        group_by=["customers.country"],
    )
    row_arm = StructuredQuery(from_table="orders", select=["orders.id", "orders.status"])
    policy = Policy(max_limit=100, max_limit_aggregate=1000)
    tables = _reflected()

    def _limit_for(first: StructuredQuery, second: StructuredQuery) -> int:
        query = StructuredQuery(
            **first.model_dump(exclude={"set_op", "limit"}),
            limit=1000,
            set_op=SetOpSpec(op="union", arms=[second]),
        )
        arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
        _stmt, limit = compile_structured_query(
            query, tables, policy, dialect="postgresql", subquery_tables=arm_tables
        )
        return limit

    assert _limit_for(aggregate_arm, aggregate_arm) == 1000
    assert _limit_for(aggregate_arm, row_arm) == 100
    assert _limit_for(row_arm, aggregate_arm) == 100


def test_set_operation_order_by_may_not_fall_back_to_a_raw_table_column():
    """A compound's ORDER BY resolves against the combined OUTPUT, and a table
    column that is not projected is not part of it. Allowing the table fallback
    here would reference `orders.status` from outside the derived table, which
    SQLAlchemy resolves by adding `orders` to the FROM clause — turning the
    statement into an unasked-for cartesian product over the whole table."""
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        set_op=SetOpSpec(
            op="union", arms=[StructuredQuery(from_table="customers", select=["customers.id"])]
        ),
        order_by=[{"col": "orders.status"}],
    )
    tables = _reflected()
    arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
    with pytest.raises(QueryValidationError, match="Unknown column or alias"):
        compile_structured_query(
            query, tables, Policy(), dialect="postgresql", subquery_tables=arm_tables
        )


def test_an_arm_that_was_not_schema_validated_fails_closed():
    """Defence in depth against a future caller of the compiler that forgets to
    thread `subquery_tables`: an arm with no reflected tables raises rather than
    silently compiling against the outer query's."""
    query = _union(StructuredQuery(from_table="customers", select=["customers.id"]))
    with pytest.raises(QueryValidationError, match="was not schema-validated"):
        compile_structured_query(query, _reflected(), Policy(), dialect="postgresql")


# --------------------------------------------------------------------------- #
# An arm resolves against its own tables, never an outer scope's               #
# --------------------------------------------------------------------------- #


async def _fake_load(connection_id, table_name, table_connection):
    """Reflect any table name with a permissive column set, so these tests
    exercise the SCOPE rules rather than a missing-column rejection."""
    return sa.Table(
        table_name,
        sa.MetaData(),
        sa.Column("id", sa.Integer),
        sa.Column("customer_id", sa.Integer),
    )


@pytest.mark.asyncio
async def test_an_arm_cannot_reference_the_other_arms_table():
    """Arms are independent scopes, so a column ref in arm 2 that names arm 1's
    table is undeclared there — the same rejection that makes an `IN (subquery)`
    structurally uncorrelated."""
    query = _union(
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            where=Predicate(col="orders.id", op="eq", value=1),
        )
    )
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        with pytest.raises(QueryValidationError, match="undeclared|only use"):
            await sv.validate_schema(query, connection_id=_CONN)


@pytest.mark.asyncio
async def test_an_arm_may_not_join_across_connections_outside_the_join_group():
    """The join_group rule is enforced per scope, so an arm reaching another
    connection is checked exactly as the outer query would be — an arm is a
    sibling top-level SELECT, so it gets the outer query's rule (a join_group
    check), not the stricter single-connection rule an `IN (subquery)` gets."""
    registry = ConnectionRegistry(
        {
            "demo": ConnectionProfile(
                id="demo",
                dialect="postgresql",
                connection_string="postgresql+asyncpg://u:p@localhost/demo",
            ),
            "warehouse": ConnectionProfile(
                id="warehouse",
                dialect="postgresql",
                connection_string="postgresql+asyncpg://u:p@localhost/warehouse",
                join_group="elsewhere",
            ),
        }
    )
    set_registry(registry)
    query = _union(
        StructuredQuery(
            from_table="customers",
            select=["customers.id"],
            joins=[
                {
                    "table": "ledger",
                    "on": ["customers.id", "ledger.customer_id"],
                    "connection": "warehouse",
                }
            ],
        )
    )
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_fake_load)):
        with pytest.raises(QueryValidationError, match="cross-connection join"):
            await sv.validate_schema(query, connection_id=_CONN)


def test_set_operation_order_by_resolves_the_column_the_caller_named():
    """The defect this pins returned a DIFFERENT ROW SET, silently, on every
    dialect — the worst failure class this repo recognises.

    The outer alias map used to be keyed by output NAME, taken from arm 1's own
    alias map. When two projections share a base name SQLAlchemy disambiguates the
    derived table's keys (`id`, `id_1`) but both refs still report `.name == "id"`,
    so `ORDER BY orders.id` bound to `customers.id`. Because ORDER BY feeds LIMIT,
    the caller received rows chosen by a column they did not name. The map is now
    positional, which is sound because the derived table's columns ARE arm 1's
    select list in order.
    """
    query = StructuredQuery(
        from_table="customers",
        select=["customers.id", "orders.id"],
        joins=[{"table": "orders", "on": ["customers.tenant", "orders.tenant"]}],
        set_op=SetOpSpec(
            op="union",
            arms=[StructuredQuery(from_table="orders", select=["orders.id", "orders.id"])],
        ),
        order_by=[{"col": "orders.id", "dir": "desc"}],
    )
    tables = _reflected()
    arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
    stmt, _limit = compile_structured_query(
        query, tables, Policy(), dialect="postgresql", subquery_tables=arm_tables
    )
    rendered = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    # The SECOND derived column (arm 1's `orders.id`), not the first.
    assert "ORDER BY anon_1.id_1 DESC" in rendered, rendered


def test_set_operation_order_by_accepts_the_written_table_column_ref():
    """The written-ref half of the alias map: a caller may order by the exact
    string they put in `select` (`"orders.id"`), not only by the bare output name.
    Deleting that loop leaves every other set-op test green while this ordinary
    spelling starts raising `Unknown column or alias`."""
    query = _union(
        StructuredQuery(from_table="customers", select=["customers.id"]),
        order_by=[{"col": "orders.id"}],
    )
    tables = _reflected()
    arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
    stmt, _limit = compile_structured_query(
        query, tables, Policy(), dialect="postgresql", subquery_tables=arm_tables
    )
    assert "ORDER BY anon_1.id" in str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_offset_is_applied_to_the_combined_result():
    """`offset` on a set operation had no test at any tier; deleting the branch
    broke nothing."""
    query = _union(
        StructuredQuery(from_table="customers", select=["customers.id"]),
        order_by=[{"col": "id"}],
        offset=7,
    )
    tables = _reflected()
    arm_tables = {id(arm): tables for arm in sv.iter_set_op_arms(query)}
    stmt, _limit = compile_structured_query(
        query, tables, Policy(), dialect="postgresql", subquery_tables=arm_tables
    )
    assert "OFFSET 7" in str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_one_arm_policy_also_disables_set_operations():
    """The other half of the `< 2` message branch: a set operation always has at
    least two arms, so `1` disables it exactly as `0` does."""
    query = _union(StructuredQuery(from_table="customers", select=["customers.id"]))
    with pytest.raises(PolicyViolationError, match="set operations are disabled"):
        validate_policy(query, Policy(max_set_op_arms=1), _CONN)


def test_the_item_118_fan_out_refusal_applies_to_every_arm():
    """`_compile_set_operation`'s docstring claims each arm inherits item 118's
    refusal of a join that can inflate a k-anonymity count. Untested until now —
    and that claim is the whole reason arms go through `_compile_scope_body`."""
    fanning_arm = StructuredQuery(
        from_table="customers",
        select=["customers.country", {"fn": "count", "col": "*", "as": "n"}],
        # `orders.tenant` is not unique, so this join can match many rows per row.
        joins=[{"table": "orders", "on": ["customers.tenant", "orders.tenant"]}],
        group_by=["customers.country"],
    )
    safe_arm = StructuredQuery(
        from_table="orders",
        select=["orders.status", {"fn": "count", "col": "*", "as": "n"}],
        group_by=["orders.status"],
    )
    query = StructuredQuery(
        **safe_arm.model_dump(exclude={"set_op"}),
        set_op=SetOpSpec(op="union", arms=[fanning_arm]),
    )
    with pytest.raises(PolicyViolationError, match="can match more than one row"):
        _compile(query, Policy(min_group_size=5))


@pytest.mark.asyncio
async def test_a_non_existent_column_in_a_later_arm_is_rejected_pre_database():
    """The plan's "schema-resolved" Definition-of-Done box asks for a non-existent
    COLUMN to be rejected before any DB touch. The other schema test proves only
    undeclared-TABLE rejection, because its fake reflection is permissive."""
    query = _union(StructuredQuery(from_table="customers", select=["customers.not_a_column"]))

    async def _strict_load(connection_id, table_name, table_connection):
        return sa.Table(table_name, sa.MetaData(), sa.Column("id", sa.Integer))

    with patch.object(sv, "_load_table", AsyncMock(side_effect=_strict_load)):
        with pytest.raises(QueryValidationError, match="not_a_column"):
            await sv.validate_schema(query, connection_id=_CONN)


# --------------------------------------------------------------------------- #
# Arm select-type compatibility (the cross-dialect divergence it closes)        #
# --------------------------------------------------------------------------- #


async def _typed_load(connection_id, table_name, table_connection):
    return sa.Table(
        table_name,
        sa.MetaData(),
        sa.Column("id", sa.Integer),
        sa.Column("name", sa.String),
        sa.Column("amount", sa.Numeric(10, 2)),
        sa.Column("created_at", sa.DateTime),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arm_two_select",
    [
        ["customers.name"],  # text vs the integer arm 1 projects
        [{"expr": {"cast": {"col": "customers.id"}, "to": "text"}, "as": "id"}],
        [{"expr": {"literal": "label"}, "as": "id"}],
        ["customers.created_at"],  # temporal vs numeric
    ],
)
async def test_arms_disagreeing_on_type_are_rejected_before_the_database(arm_two_select):
    """Postgres refuses a mismatched union outright; SQL Server applies data-type
    precedence and may silently convert one side and return rows (both measured on
    live servers). Refused on every dialect so the same AST behaves the same way
    everywhere."""
    query = _union(StructuredQuery.model_validate({"from": "customers", "select": arm_two_select}))
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_typed_load)):
        with pytest.raises(QueryValidationError, match="disagree on the type"):
            await sv.validate_schema(query, connection_id=_CONN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arm_two_select",
    [
        ["customers.id"],  # integer vs integer
        ["customers.amount"],  # NUMERIC vs INTEGER — same family, unions fine
        [{"expr": {"literal": 7}, "as": "id"}],
        # Unknown-typed: arithmetic is left to the database rather than guessed.
        [
            {
                "expr": {"op": "+", "left": {"col": "customers.id"}, "right": {"literal": 1}},
                "as": "id",
            }
        ],
    ],
)
async def test_compatible_or_unknown_arm_types_are_allowed(arm_two_select):
    """The other half of the rule, and the one that keeps it from over-rejecting:
    integer/numeric and date/timestamp are the SAME coarse family because both
    backends union them happily, and an unknown family always means allow."""
    query = _union(StructuredQuery.model_validate({"from": "customers", "select": arm_two_select}))
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_typed_load)):
        await sv.validate_schema(query, connection_id=_CONN)


@pytest.mark.asyncio
async def test_arm_types_are_checked_inside_a_nested_subquerys_set_operation():
    """The type rule walks every scope, not only the top-level set operation."""
    nested = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": ["customers.id"],
            "set_op": {
                "op": "union",
                "arms": [{"from": "customers", "select": ["customers.name"]}],
            },
        }
    )
    query = StructuredQuery(
        from_table="orders",
        select=["orders.id"],
        where=Predicate(col="orders.id", op="in", value_subquery=nested),
    )
    with patch.object(sv, "_load_table", AsyncMock(side_effect=_typed_load)):
        with pytest.raises(QueryValidationError, match="disagree on the type"):
            await sv.validate_schema(query, connection_id=_CONN)
