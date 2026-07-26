"""Unit tests for compiling a StructuredQuery + Policy into SQLAlchemy Core."""

from __future__ import annotations

from typing import Dict

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite

from querygate.compiler.sqlalchemy_compiler import clamp_limit, compile_structured_query
from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import MandatoryRowFilter, Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    ArrayAggSelectItem,
    DateBucketSelectItem,
    JoinSpec,
    OrderBySpec,
    PercentileContSelectItem,
    Predicate,
    StringAggSelectItem,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)


def _make_tables() -> Dict[str, sa.Table]:
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
        sa.Column("created_at", sa.DateTime),
    )
    return {"customers": customers, "orders": orders}


class TestClampLimit:
    def test_default_and_cap(self):
        policy = Policy(default_limit=50, max_limit=100)
        assert clamp_limit(None, policy) == 50
        assert clamp_limit(10, policy) == 10
        assert clamp_limit(500, policy) == 100

    def test_aggregate_uses_higher_cap(self):
        policy = Policy(default_limit=50, max_limit=100, max_limit_aggregate=1000)
        assert clamp_limit(500, policy, is_aggregate=False) == 100
        assert clamp_limit(500, policy, is_aggregate=True) == 500
        assert clamp_limit(5000, policy, is_aggregate=True) == 1000


class TestCompiler:
    def test_simple_select_sql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders", select=["orders.id", "orders.status"], limit=25
        )
        stmt, limit = compile_structured_query(query, tables, Policy())
        assert limit == 25
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders" in compiled
        assert "25" in compiled

    def test_left_join_and_like_or(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.status", "customers.name"],
            joins=[
                JoinSpec(table="customers", type="left", on=["orders.customer_id", "customers.id"])
            ],
            where=WhereGroup(
                or_terms=[
                    Predicate(col="customers.name", op="like", value="%Ada%"),
                    Predicate(col="orders.status", op="eq", value="completed"),
                ]
            ),
            order_by=[OrderBySpec(col="customers.name", dir="asc")],
            limit=20,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "JOIN" in compiled.upper()
        assert "LIKE" in compiled.upper() or "%Ada%" in compiled

    def test_group_by_having_aggregate(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                AggregateSelectItem(fn="count", col="*", alias="order_count"),
            ],
            group_by=["orders.customer_id"],
            having=Predicate(col="order_count", op="gte", value=2),
            order_by=[OrderBySpec(col="order_count", dir="desc")],
            limit=50,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "GROUP BY" in compiled.upper()
        assert "HAVING" in compiled.upper() or "count" in compiled.lower()
        assert "ORDER BY" in compiled.upper()
        assert "DESC" in compiled.upper()

    def test_between_and_in(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(
                and_terms=[
                    Predicate(col="orders.id", op="between", value=[1, 10]),
                    Predicate(col="orders.status", op="in", value=["completed", "pending"]),
                ]
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "BETWEEN" in compiled.upper() or "1" in compiled

    def test_date_bucket_postgres_uses_date_trunc(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                DateBucketSelectItem(col="orders.created_at", granularity="month", alias="month"),
                AggregateSelectItem(fn="count", col="*", alias="cnt"),
            ],
            group_by=["month"],
            order_by=[OrderBySpec(col="month", dir="asc")],
            limit=50,
        )
        stmt, limit = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "date_trunc" in compiled.lower()
        assert "GROUP BY" in compiled.upper()
        assert limit == 50

    def test_date_bucket_mssql_uses_dateadd_datediff(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[DateBucketSelectItem(col="orders.created_at", granularity="day")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "DATEADD" in compiled.upper()
        assert "DATEDIFF" in compiled.upper()
        assert "bucket_created_at_day" in compiled

    def test_top_n_ranks_within_partition(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.customer_id", "orders.total_amount"],
            top_n=TopNSpec(
                partition_by=["orders.customer_id"],
                order_by=[OrderBySpec(col="orders.total_amount", dir="desc")],
                n=2,
            ),
            limit=50,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "OVER" in compiled.upper()
        assert "ROW_NUMBER" in compiled.upper()
        assert "__rank" in compiled

    def test_top_n_over_aggregate_group_by(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.customer_id", AggregateSelectItem(fn="count", col="*", alias="cnt")],
            group_by=["orders.customer_id"],
            top_n=TopNSpec(partition_by=[], order_by=[OrderBySpec(col="cnt", dir="desc")], n=3),
            limit=50,
        )
        stmt, limit = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "GROUP BY" in compiled.upper()
        assert "OVER" in compiled.upper()
        assert limit == 50

    def test_mandatory_row_filter_applied_when_table_in_graph(self):
        tables = _make_tables()
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="completed")
            ]
        )
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "completed" in compiled

    def test_mandatory_row_filter_skipped_when_table_not_in_graph(self):
        tables = {"customers": _make_tables()["customers"]}
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="completed")
            ]
        )
        query = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
        # Must not raise even though "orders" isn't part of this query's graph.
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "completed" not in compiled

    def test_mandatory_row_filter_resolves_from_principal_claim(self):
        tables = _make_tables()
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", from_claim="order_status")
            ]
        )
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        principal = Principal(subject="agent-a", claims={"order_status": "shipped"})
        stmt, _ = compile_structured_query(query, tables, policy, principal=principal)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "shipped" in compiled

    def test_mandatory_row_filter_from_claim_without_principal_raises(self):
        tables = _make_tables()
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", from_claim="order_status")
            ]
        )
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        with pytest.raises(PolicyViolationError, match="order_status"):
            compile_structured_query(query, tables, policy)

    def test_mandatory_row_filter_from_claim_missing_from_principal_raises(self):
        tables = _make_tables()
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", from_claim="order_status")
            ]
        )
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        principal = Principal(subject="agent-a")  # no claims at all
        with pytest.raises(PolicyViolationError, match="order_status"):
            compile_structured_query(query, tables, policy, principal=principal)

    def test_coalesce_renders_on_postgres_and_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "fn": "coalesce",
                    "args": [{"col": "orders.total_amount"}, {"literal": 0}],
                    "as": "amount",
                }
            ],
            limit=5,
        )
        for dialect in ("postgresql", "mssql"):
            stmt, _ = compile_structured_query(query, tables, Policy(), dialect=dialect)
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            assert "coalesce" in compiled.lower()
            assert "AS amount" in compiled

    def test_lower_upper_trim_render(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"fn": "lower", "args": [{"col": "orders.status"}], "as": "lc"},
                {"fn": "upper", "args": [{"col": "orders.status"}], "as": "uc"},
                {"fn": "trim", "args": [{"col": "orders.status"}], "as": "tc"},
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "lower(orders.status)" in compiled
        assert "upper(orders.status)" in compiled
        assert "trim(orders.status)" in compiled

    def test_scalar_function_default_alias(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[{"fn": "lower", "args": [{"col": "orders.status"}]}],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "AS lower_status" in compiled

    def test_case_with_else_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {"col": "orders.status", "op": "eq", "value": "completed"},
                            "then": {"literal": "Done"},
                        }
                    ],
                    "else": {"literal": "Open"},
                    "as": "label",
                }
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "CASE WHEN" in compiled.upper()
        assert "ELSE" in compiled.upper()
        assert "AS label" in compiled

    def test_case_without_else_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {"col": "orders.status", "op": "eq", "value": "completed"},
                            "then": {"literal": "Done"},
                        }
                    ],
                    "as": "label",
                }
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "CASE WHEN" in compiled.upper()
        assert "ELSE" not in compiled.upper()

    def test_case_group_by_alias_referenceable(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {"col": "orders.status", "op": "eq", "value": "completed"},
                            "then": {"literal": "Done"},
                        }
                    ],
                    "else": {"literal": "Open"},
                    "as": "label",
                },
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["label"],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "GROUP BY" in compiled.upper()

    def test_where_predicate_col_fn_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "orders.status"}]},
                op="eq",
                value="active",
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "lower(orders.status) = 'active'" in compiled

    def test_having_predicate_col_fn_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.status",
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["orders.status"],
            having=Predicate(
                col_fn={
                    "fn": "coalesce",
                    "args": [{"col": "orders.total_amount"}, {"literal": 0}],
                },
                op="gt",
                value=0,
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "coalesce(orders.total_amount, 0) >" in compiled

    def test_case_when_predicate_with_col_fn_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {
                                "col_fn": {"fn": "lower", "args": [{"col": "orders.status"}]},
                                "op": "eq",
                                "value": "completed",
                            },
                            "then": {"literal": "Done"},
                        }
                    ],
                    "else": {"literal": "Open"},
                    "as": "label",
                }
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "lower(orders.status)" in compiled
        assert "CASE WHEN" in compiled.upper()

    def test_searched_having_or_group_renders(self):
        """item 99: HAVING is a WhereNode, so OR-logic over aggregate conditions
        renders as a single boolean HAVING clause."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.status",
                AggregateSelectItem(fn="sum", col="orders.total_amount", alias="s"),
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["orders.status"],
            having=WhereGroup(
                or_terms=[
                    Predicate(col="s", op="gt", value=10),
                    Predicate(col="n", op="lt", value=3),
                ]
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "HAVING" in compiled
        # The two aggregate conditions are OR-combined inside one HAVING clause.
        having_clause = compiled.split("HAVING", 1)[1]
        assert " OR " in having_clause

    def test_searched_case_and_condition_renders(self):
        """item 99: a searched-CASE condition is a full WhereNode — multiple
        conditions combine with AND/OR inside one WHEN."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {
                                "and": [
                                    {"col": "orders.status", "op": "eq", "value": "completed"},
                                    {"col": "orders.total_amount", "op": "gt", "value": 100},
                                ]
                            },
                            "then": {"literal": "big-done"},
                        }
                    ],
                    "else": {"literal": "other"},
                    "as": "label",
                }
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "CASE WHEN" in compiled
        case_clause = compiled.split("CASE WHEN", 1)[1].split("END", 1)[0]
        assert " AND " in case_clause

    def test_composite_join_key_ands_both_conditions(self):
        metadata = sa.MetaData()
        orders = sa.Table(
            "orders",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tenant_id", sa.Integer),
        )
        order_items = sa.Table(
            "order_items",
            metadata,
            sa.Column("order_id", sa.Integer),
            sa.Column("tenant_id", sa.Integer),
            sa.Column("sku", sa.String(20)),
        )
        tables = {"orders": orders, "order_items": order_items}
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "order_items.sku"],
            joins=[
                JoinSpec(
                    table="order_items",
                    on=["orders.tenant_id", "order_items.tenant_id"],
                    extra_on=[["orders.id", "order_items.order_id"]],
                )
            ],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders.tenant_id = order_items.tenant_id" in compiled
        assert "orders.id = order_items.order_id" in compiled
        assert "AND" in compiled.upper()

    def test_stddev_variance_render_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                AggregateSelectItem(fn="stddev", col="orders.total_amount", alias="sd"),
                AggregateSelectItem(fn="variance", col="orders.total_amount", alias="var"),
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "stddev(" in compiled
        assert "variance(" in compiled

    def test_stddev_variance_render_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                AggregateSelectItem(fn="stddev", col="orders.total_amount", alias="sd"),
                AggregateSelectItem(fn="variance", col="orders.total_amount", alias="var"),
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "STDEV(" in compiled
        assert "VAR(" in compiled

    def test_string_agg_render_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                StringAggSelectItem(col="orders.status", delimiter=", ", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "string_agg(orders.status, ', ')" in compiled

    def test_string_agg_render_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                StringAggSelectItem(col="orders.status", delimiter=", ", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "STRING_AGG(" in compiled

    def test_string_agg_default_alias(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[StringAggSelectItem(col="orders.status", delimiter=", ")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "string_agg_status" in compiled

    def test_string_agg_is_treated_as_aggregate_for_having(self):
        """A group_by + string_agg + having(on the string_agg alias) shape
        must compile — this only works if StringAggSelectItem participates
        in the same "is_aggregate" detection AggregateSelectItem does."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                StringAggSelectItem(col="orders.status", delimiter=", ", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "HAVING" in compiled

    def test_array_agg_render_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                ArrayAggSelectItem(col="orders.status", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "array_agg(orders.status)" in compiled

    def test_array_agg_rejected_on_mssql(self):
        """MSSQL has no array/collection type — array_agg must be rejected
        outright, not emulated, unlike string_agg which renders on MSSQL."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                ArrayAggSelectItem(col="orders.status", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        with pytest.raises(QueryValidationError, match="array/collection type"):
            compile_structured_query(query, tables, Policy(), dialect="mssql")

    def test_array_agg_default_alias(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[ArrayAggSelectItem(col="orders.status")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "array_agg_status" in compiled

    def test_array_agg_is_treated_as_aggregate_for_having(self):
        """A group_by + array_agg + having(on the array_agg alias) shape
        must compile — this only works if ArrayAggSelectItem participates
        in the same "is_aggregate" detection AggregateSelectItem does."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                ArrayAggSelectItem(col="orders.status", alias="statuses"),
            ],
            group_by=["orders.customer_id"],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "HAVING" in compiled

    def test_percentile_cont_render_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                PercentileContSelectItem(col="orders.total_amount", fraction=0.5, alias="median"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "percentile_cont(0.5)" in compiled
        assert "within group" in compiled

    def test_percentile_cont_rejected_on_mssql(self):
        """T-SQL's PERCENTILE_CONT has no GROUP BY-compatible form — must
        be rejected outright, not emulated, same shape as array_agg."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                PercentileContSelectItem(col="orders.total_amount", fraction=0.5, alias="median"),
            ],
            group_by=["orders.customer_id"],
            limit=5,
        )
        with pytest.raises(QueryValidationError, match="analytic/window function"):
            compile_structured_query(query, tables, Policy(), dialect="mssql")

    def test_percentile_cont_default_alias(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[PercentileContSelectItem(col="orders.total_amount", fraction=0.5)],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "percentile_cont_total_amount" in compiled

    def test_percentile_cont_is_treated_as_aggregate_for_having(self):
        """A group_by + percentile_cont + having(on the percentile_cont
        alias) shape must compile — this only works if
        PercentileContSelectItem participates in the same "is_aggregate"
        detection AggregateSelectItem does."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                PercentileContSelectItem(col="orders.total_amount", fraction=0.5, alias="median"),
            ],
            group_by=["orders.customer_id"],
            having=Predicate(col="median", op="gt", value=0),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "HAVING" in compiled

    def test_order_by_nulls_last_renders_natively_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            order_by=[OrderBySpec(col="orders.status", dir="asc", nulls="last")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NULLS LAST" in compiled.upper()

    def test_order_by_nulls_rejected_on_mssql(self):
        """T-SQL has no NULLS FIRST/LAST syntax — QueryGate rejects `nulls` on
        MSSQL rather than synthesizing a CASE-bucket the AST never asked for,
        the same posture as array_agg (CLAUDE.md engine philosophy, item 74)."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            order_by=[OrderBySpec(col="orders.status", dir="asc", nulls="first")],
            limit=5,
        )
        with pytest.raises(
            QueryValidationError, match="nulls first/last ordering is not supported"
        ):
            compile_structured_query(query, tables, Policy(), dialect="mssql")

    def test_order_by_without_nulls_unaffected(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            order_by=[OrderBySpec(col="orders.status", dir="desc")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "CASE" not in compiled.upper()
        assert "DESC" in compiled.upper()

    def test_top_n_with_nulls_renders_inside_over_order_by_on_postgres(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.customer_id"],
            top_n=TopNSpec(
                order_by=[OrderBySpec(col="orders.total_amount", dir="desc", nulls="last")],
                n=3,
            ),
            limit=50,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True})).upper()
        assert "OVER" in compiled
        assert "NULLS LAST" in compiled

    def test_top_n_with_nulls_rejected_on_mssql(self):
        """The rejection covers the rank-ordering call site too, not just the
        outer ORDER BY — `nulls` anywhere the MSSQL adapter renders order terms
        is rejected (item 74)."""
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.customer_id"],
            top_n=TopNSpec(
                order_by=[OrderBySpec(col="orders.total_amount", dir="desc", nulls="last")],
                n=3,
            ),
            limit=50,
        )
        with pytest.raises(
            QueryValidationError, match="nulls first/last ordering is not supported"
        ):
            compile_structured_query(query, tables, Policy(), dialect="mssql")

    def test_not_group_renders(self):
        # SQLAlchemy simplifies NOT(col = val) to col != val at the
        # expression level (still a correct negation) rather than emitting a
        # literal NOT — assert the negated effect, not the literal keyword.
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(not_terms=Predicate(col="orders.status", op="eq", value="completed")),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "!=" in compiled

    def test_not_wrapping_and_group_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(
                not_terms=WhereGroup(
                    and_terms=[
                        Predicate(col="orders.status", op="eq", value="completed"),
                        Predicate(col="orders.total_amount", op="gt", value=100),
                    ]
                )
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NOT" in compiled.upper()
        assert "AND" in compiled.upper()

    def test_column_to_column_comparison_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.total_amount", op="gt", value_col="orders.id"),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders.total_amount > orders.id" in compiled

    def test_self_join_renders_two_aliases(self):
        metadata = sa.MetaData()
        employees = sa.Table(
            "employees",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(100)),
            sa.Column("manager_id", sa.Integer),
        )
        tables = {"e": employees.alias("e"), "m": employees.alias("m")}
        query = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.name", "m.name"],
            joins=[JoinSpec(table="employees", alias="m", on=["e.manager_id", "m.id"])],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "AS e" in compiled or "employees AS e" in compiled
        assert "AS m" in compiled or "employees AS m" in compiled
        assert "JOIN" in compiled.upper()

    def test_mandatory_row_filter_applies_to_every_self_join_alias(self):
        metadata = sa.MetaData()
        employees = sa.Table(
            "employees",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(100)),
            sa.Column("manager_id", sa.Integer),
            sa.Column("tenant", sa.String(20)),
        )
        tables = {"e": employees.alias("e"), "m": employees.alias("m")}
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="employees", column="tenant", value="acme")
            ]
        )
        query = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.name"],
            joins=[JoinSpec(table="employees", alias="m", on=["e.manager_id", "m.id"])],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert compiled.count("acme") == 2

    def test_query_level_distinct_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders", select=["orders.status"], distinct=True, limit=10
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "SELECT DISTINCT" in compiled.upper()

    def test_count_distinct_renders(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[AggregateSelectItem(fn="count", col="orders.status", distinct=True, alias="n")],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "COUNT(DISTINCT" in compiled.upper()

    def test_aggregate_query_gets_higher_limit_cap(self):
        tables = _make_tables()
        policy = Policy(default_limit=50, max_limit=100, max_limit_aggregate=1000)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.customer_id", AggregateSelectItem(fn="count", col="*", alias="cnt")],
            group_by=["orders.customer_id"],
            limit=500,
        )
        _, limit = compile_structured_query(query, tables, policy)
        assert limit == 500


class TestCrossDialectRendering:
    """TODO.md item 78 — every item-68-77 compiler code path that previously
    only had a Postgres-default (or single-dialect) render test also gets a
    dialect="mssql" one, closing the gap where a feature could render fine
    on Postgres and be unrenderable/wrong on MSSQL without any test noticing
    (exactly what caught the NULLS FIRST/LAST and stddev/variance naming
    gaps items 74/75 had to work around). Rendering-level, not live
    execution — see that item's own scope note on why.
    """

    def test_distinct_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders", select=["orders.status"], distinct=True, limit=10
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "SELECT DISTINCT" in compiled.upper()

    def test_count_distinct_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[AggregateSelectItem(fn="count", col="orders.status", distinct=True, alias="n")],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "COUNT(DISTINCT" in compiled.upper()

    def test_self_join_renders_on_mssql(self):
        metadata = sa.MetaData()
        employees = sa.Table(
            "employees",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(100)),
            sa.Column("manager_id", sa.Integer),
        )
        tables = {"e": employees.alias("e"), "m": employees.alias("m")}
        query = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.name", "m.name"],
            joins=[JoinSpec(table="employees", alias="m", on=["e.manager_id", "m.id"])],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "JOIN" in compiled.upper()
        assert "AS m" in compiled

    def test_not_group_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(
                not_terms=WhereGroup(
                    and_terms=[
                        Predicate(col="orders.status", op="eq", value="completed"),
                        Predicate(col="orders.total_amount", op="gt", value=100),
                    ]
                )
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "NOT" in compiled.upper()

    def test_value_col_comparison_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.total_amount", op="gt", value_col="orders.id"),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders.total_amount > orders.id" in compiled

    def test_lower_upper_trim_concat_render_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"fn": "lower", "args": [{"col": "orders.status"}], "as": "lc"},
                {"fn": "upper", "args": [{"col": "orders.status"}], "as": "uc"},
                {"fn": "trim", "args": [{"col": "orders.status"}], "as": "tc"},
                {
                    "fn": "concat",
                    "args": [{"col": "orders.status"}, {"literal": "!"}],
                    "as": "cc",
                },
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "lower(orders.status)" in compiled
        assert "upper(orders.status)" in compiled
        assert "trim(orders.status)" in compiled
        assert "concat(orders.status" in compiled

    def test_case_with_else_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                {
                    "when": [
                        {
                            "when": {"col": "orders.status", "op": "eq", "value": "completed"},
                            "then": {"literal": "Done"},
                        }
                    ],
                    "else": {"literal": "Open"},
                    "as": "label",
                }
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "CASE WHEN" in compiled.upper()
        assert "ELSE" in compiled.upper()

    def test_composite_join_key_renders_on_mssql(self):
        metadata = sa.MetaData()
        orders = sa.Table(
            "orders",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tenant_id", sa.Integer),
        )
        order_items = sa.Table(
            "order_items",
            metadata,
            sa.Column("order_id", sa.Integer),
            sa.Column("tenant_id", sa.Integer),
            sa.Column("sku", sa.String(20)),
        )
        tables = {"orders": orders, "order_items": order_items}
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "order_items.sku"],
            joins=[
                JoinSpec(
                    table="order_items",
                    on=["orders.tenant_id", "order_items.tenant_id"],
                    extra_on=[["orders.id", "order_items.order_id"]],
                )
            ],
            limit=10,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders.tenant_id = order_items.tenant_id" in compiled
        assert "orders.id = order_items.order_id" in compiled
        assert "AND" in compiled.upper()

    def test_predicate_col_fn_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "orders.status"}]},
                op="eq",
                value="active",
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "lower(orders.status) = 'active'" in compiled

    def test_having_predicate_col_fn_renders_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.status", AggregateSelectItem(fn="count", col="*", alias="n")],
            group_by=["orders.status"],
            having=Predicate(
                col_fn={
                    "fn": "coalesce",
                    "args": [{"col": "orders.total_amount"}, {"literal": 0}],
                },
                op="gt",
                value=0,
            ),
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "coalesce(orders.total_amount, 0) >" in compiled


class TestMinGroupSize:
    """k-anonymity guardrail (TODO.md item 88): Policy.min_group_size injects
    `HAVING count(*) >= k` into aggregate queries so a caller can't single out a
    group backed by fewer than k rows. Only aggregate queries are affected."""

    def test_injects_having_on_grouped_aggregate(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["orders.customer_id"],
        )
        stmt, _ = compile_structured_query(query, tables, Policy(min_group_size=5))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "HAVING" in compiled.upper()
        assert "count(*) >= 5" in compiled.lower()

    def test_injects_having_on_ungrouped_single_group_aggregate(self):
        # No GROUP BY: the single implicit group (all rows matching WHERE) must
        # still meet the floor, so a count over a razor-thin filter is suppressed.
        tables = _make_tables()
        query = StructuredQuery(
            from_table="customers",
            select=[AggregateSelectItem(fn="count", col="*", alias="n")],
            where=Predicate(col="customers.id", op="eq", value=1),
        )
        stmt, _ = compile_structured_query(query, tables, Policy(min_group_size=5))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "count(*) >= 5" in compiled.lower()

    def test_not_applied_to_plain_non_aggregate_select(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "customers.name"],
            where=Predicate(col="customers.id", op="eq", value=1),
        )
        stmt, _ = compile_structured_query(query, tables, Policy(min_group_size=5))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "having" not in compiled.lower()

    def test_combines_with_caller_having(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["orders.customer_id"],
            having=Predicate(col="n", op="gte", value=2),
        )
        stmt, _ = compile_structured_query(query, tables, Policy(min_group_size=5))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # Both the caller's own HAVING and the injected floor are present.
        assert "count(*) >= 5" in compiled.lower()
        assert ">= 2" in compiled

    def test_none_is_a_noop(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=[
                "orders.customer_id",
                AggregateSelectItem(fn="count", col="*", alias="n"),
            ],
            group_by=["orders.customer_id"],
        )
        stmt, _ = compile_structured_query(query, tables, Policy(min_group_size=None))
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "having" not in compiled.lower()


class TestExpressionSubstrate:
    """TODO.md item 100 — the bounded scalar Expression substrate.

    Rendering only; the safety properties (visitor coverage, allow/deny, masking,
    caps) live in test_reference_visitor.py / test_policy_validation.py /
    tests/security, and the end-to-end *values* in
    tests/integration/test_expression_end_to_end.py.
    """

    # Compile against the REAL per-dialect SQL compiler, not the generic one:
    # part of item 100's rendering (CAST target type names) is chosen by
    # SQLAlchemy's dialect compiler rather than by our adapter, and a generic
    # compile would silently show identical SQL for every dialect — the exact
    # "renders fine, breaks live" blind spot items 75/82 flagged.
    _DIALECT_COMPILERS = {
        "postgresql": postgresql.dialect(),
        "mssql": mssql.dialect(),
        "sqlite": sqlite.dialect(),
    }

    @classmethod
    def _sql(
        cls, query: StructuredQuery, dialect: str = "postgresql", policy: Policy = None
    ) -> str:
        stmt, _ = compile_structured_query(
            query, _make_tables(), policy or Policy(), dialect=dialect
        )
        return str(
            stmt.compile(
                dialect=cls._DIALECT_COMPILERS[dialect],
                compile_kwargs={"literal_binds": True},
            )
        )

    def test_arithmetic_projection(self):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {
                            "op": "*",
                            "left": {"col": "orders.total_amount"},
                            "right": {"literal": 2},
                        },
                        "as": "doubled",
                    }
                ],
            }
        )
        sql = self._sql(query)
        assert "orders.total_amount * 2 AS doubled" in sql

    def test_division_is_guarded_with_nullif_on_every_dialect(self):
        """2026-07-25 Decision Log: `/` renders `left / NULLIF(right, 0)` so a
        zero denominator yields NULL identically everywhere, rather than
        inheriting Postgres's hard error and MSSQL's XACT_ABORT transaction
        abort."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {
                            "op": "/",
                            "left": {"col": "orders.total_amount"},
                            "right": {"col": "orders.id"},
                        },
                        "as": "ratio",
                    }
                ],
            }
        )
        for dialect in ("postgresql", "mssql", "sqlite"):
            sql = self._sql(query, dialect=dialect).lower()
            assert "nullif(orders.id, 0)" in sql, dialect

    def test_aggregate_over_an_expression(self):
        """Regression bar row 1: SUM(quantity * unit_price)."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "fn": "sum",
                        "arg": {
                            "op": "*",
                            "left": {"col": "orders.total_amount"},
                            "right": {"literal": 3},
                        },
                        "as": "revenue",
                    }
                ],
            }
        )
        assert "sum(orders.total_amount * 3) AS revenue" in self._sql(query)

    def test_conditional_aggregation(self):
        """Regression bar row 2: SUM(CASE WHEN status='paid' THEN amount ELSE 0 END)."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "fn": "sum",
                        "arg": {
                            "when": [
                                {
                                    "when": {"col": "orders.status", "op": "eq", "value": "paid"},
                                    "then": {"col": "orders.total_amount"},
                                }
                            ],
                            "else": {"literal": 0},
                        },
                        "as": "paid_total",
                    }
                ],
            }
        )
        sql = self._sql(query)
        assert "sum(CASE WHEN (orders.status = 'paid') THEN orders.total_amount ELSE 0 END)" in sql

    def test_nested_functions(self):
        query = StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": [
                    {
                        "expr": {
                            "fn": "lower",
                            "args": [{"fn": "trim", "args": [{"col": "customers.name"}]}],
                        },
                        "as": "normalized",
                    }
                ],
            }
        )
        assert "lower(trim(customers.name)) AS normalized" in self._sql(query)

    def test_literal_operands_are_bound_not_evaluated_in_python(self):
        """Two literal operands must be combined by the DATABASE. If the compiler
        used the raw Python values, `'a' * 2` would silently become the string
        'aa' here instead of the type error the database should reject."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {"op": "*", "left": {"literal": "a"}, "right": {"literal": 2}},
                        "as": "oops",
                    }
                ],
            }
        )
        sql = self._sql(query)
        assert "'a' * 2" in sql
        assert "'aa'" not in sql

    def test_cast_renders_per_dialect_type_names(self):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [{"expr": {"cast": {"col": "orders.id"}, "to": "text"}, "as": "id_text"}],
            }
        )
        # `to: text` renders NVARCHAR(max) on MSSQL. NOTE: this asserts the
        # rendering only. The *reason* for choosing Unicode over Text is that a
        # connected server renders Text as VARCHAR(max), which silently mangles
        # non-ASCII — invisible in SQL text, so it is proven by the round-trip
        # assertion in tests/integration/test_mssql_expression_substrate.py.
        assert "CAST(orders.id AS VARCHAR)" in self._sql(query, dialect="postgresql")
        assert "CAST(orders.id AS NVARCHAR(max))" in self._sql(query, dialect="mssql")

    @pytest.mark.parametrize(
        "fn_name,args,expected",
        [
            ("length", [{"col": "customers.name"}], {"postgresql": "length(", "mssql": "LEN("}),
            (
                "ceil",
                [{"col": "orders.total_amount"}],
                {"postgresql": "ceil(", "mssql": "CEILING("},
            ),
            (
                # The syntax genuinely differs: Postgres takes the SQL-standard
                # SUBSTRING(x FROM a FOR b), T-SQL only the comma form, and the
                # `substring` name is only a SQLite alias since 3.34 so the
                # internal dialect uses the always-present substr().
                "substring",
                [{"col": "customers.name"}, {"literal": 1}, {"literal": 3}],
                {
                    "postgresql": "SUBSTRING(customers.name FROM 1 FOR 3)",
                    "mssql": "substring(customers.name, 1, 3)",
                    "sqlite": "substr(customers.name, 1, 3)",
                },
            ),
        ],
    )
    def test_divergent_functions_go_through_the_dialect_adapter(self, fn_name, args, expected):
        """The four functions whose SQL genuinely differs are rendered by a
        DialectAdapter method, never an inline `if dialect ==` in the compiler."""
        query = StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": [{"expr": {"fn": fn_name, "args": args}, "as": "v"}],
            }
        )
        for dialect, fragment in expected.items():
            assert fragment in self._sql(query, dialect=dialect), (fn_name, dialect)

    def test_two_argument_round_casts_to_numeric_on_postgres_only(self):
        """Postgres has no round(double precision, integer) — only
        round(numeric, integer) — so the adapter casts. That is mechanical
        per-dialect rendering of the same operation, not synthesized structure."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {
                            "fn": "round",
                            "args": [{"col": "orders.total_amount"}, {"literal": 2}],
                        },
                        "as": "rounded",
                    }
                ],
            }
        )
        assert "round(CAST(orders.total_amount AS NUMERIC), 2)" in self._sql(query)
        assert "ROUND(orders.total_amount, 2)" in self._sql(query, dialect="mssql")

    def test_one_argument_round_gets_mssqls_required_length_argument(self):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {"fn": "round", "args": [{"col": "orders.total_amount"}]},
                        "as": "rounded",
                    }
                ],
            }
        )
        assert "ROUND(orders.total_amount, 0)" in self._sql(query, dialect="mssql")
        assert "round(orders.total_amount)" in self._sql(query)

    def test_expression_predicate_on_both_sides(self):
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id"],
                "where": {
                    "expr": {
                        "op": "*",
                        "left": {"col": "orders.total_amount"},
                        "right": {"literal": 2},
                    },
                    "op": "gt",
                    "value_expr": {
                        "op": "+",
                        "left": {"col": "orders.id"},
                        "right": {"literal": 1},
                    },
                },
            }
        )
        assert "orders.total_amount * 2 > orders.id + 1" in self._sql(query)

    def test_group_by_an_expression_select_alias(self):
        """Computed group keys are expressed by projecting the expression and
        grouping by its alias — the same route date_bucket has always used, so
        the engine needs no second inline grammar for group keys."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {
                        "expr": {
                            "when": [
                                {
                                    "when": {
                                        "col": "orders.total_amount",
                                        "op": "gt",
                                        "value": 100,
                                    },
                                    "then": {"literal": "big"},
                                }
                            ],
                            "else": {"literal": "small"},
                        },
                        "as": "size_bucket",
                    },
                    {"fn": "count", "col": "*", "as": "n"},
                ],
                "group_by": ["size_bucket"],
            }
        )
        sql = self._sql(query)
        assert "GROUP BY" in sql and "size_bucket" in sql

    def test_aggregate_col_sugar_still_renders_identically(self):
        """The `col` spelling normalizes to `arg`, so it must produce the exact
        same SQL as the canonical form (2026-07-25 Decision Log)."""
        sugar = StructuredQuery.model_validate(
            {"from": "orders", "select": [{"fn": "sum", "col": "orders.total_amount"}]}
        )
        canonical = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {"fn": "sum", "arg": {"col": "orders.total_amount"}, "as": "sum_total_amount"}
                ],
            }
        )
        assert self._sql(sugar) == self._sql(canonical)

    def test_count_distinct_over_an_expression(self):
        query = StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": [
                    {
                        "fn": "count",
                        "arg": {"fn": "lower", "args": [{"col": "customers.name"}]},
                        "distinct": True,
                        "as": "distinct_names",
                    }
                ],
            }
        )
        assert "count(DISTINCT lower(customers.name))" in self._sql(query)
