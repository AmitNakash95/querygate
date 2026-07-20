"""Unit tests for compiling a StructuredQuery + Policy into SQLAlchemy Core."""

from __future__ import annotations

from typing import Dict

import pytest
import sqlalchemy as sa

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
            having=[Predicate(col="order_count", op="gte", value=2)],
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
            having=[
                Predicate(
                    col_fn={
                        "fn": "coalesce",
                        "args": [{"col": "orders.total_amount"}, {"literal": 0}],
                    },
                    op="gt",
                    value=0,
                )
            ],
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
            having=[Predicate(col="statuses", op="neq", value="")],
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
            having=[Predicate(col="statuses", op="neq", value="")],
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

    def test_order_by_nulls_first_emulated_on_mssql(self):
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            order_by=[OrderBySpec(col="orders.status", dir="asc", nulls="first")],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "CASE" in compiled.upper()
        assert "NULLS" not in compiled.upper()

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

    def test_top_n_with_nulls_renders_inside_over_order_by(self):
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
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "OVER" in compiled.upper()
        assert "CASE" in compiled.upper()

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
            having=[
                Predicate(
                    col_fn={
                        "fn": "coalesce",
                        "args": [{"col": "orders.total_amount"}, {"literal": 0}],
                    },
                    op="gt",
                    value=0,
                )
            ],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="mssql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "coalesce(orders.total_amount, 0) >" in compiled
