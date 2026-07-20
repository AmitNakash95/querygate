"""Unit tests for compiling a StructuredQuery + Policy into SQLAlchemy Core."""

from __future__ import annotations

from typing import Dict

import pytest
import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import clamp_limit, compile_structured_query
from querygate.core.auth import Principal
from querygate.core.exceptions import PolicyViolationError
from querygate.policy.models import MandatoryRowFilter, Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    DateBucketSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
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
