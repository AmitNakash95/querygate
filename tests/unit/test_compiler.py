"""Unit tests for compiling a StructuredQuery + Policy into SQLAlchemy Core."""

from __future__ import annotations

from typing import Dict

import pydantic
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite

from querygate.compiler.sqlalchemy_compiler import clamp_limit, compile_structured_query
from querygate.connections.models import ConnectionProfile
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
    WindowBound,
    WindowSelectItem,
)
from querygate.validation.schema_validation import _validate_top_n


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

    def test_grouped_top_n_binds_same_named_projections_positionally(self):
        """A derived table disambiguates its keys, while both columns keep the
        name ``id``. Name-based lookup selected ``customers.id`` twice and also
        ranked by it when the caller named ``orders.id``.

        All three derived-table relationships `_apply_top_n` builds are asserted
        here, because each fails independently: the rank references
        (``agg_alias_map``), the outer projection (the positional ``outer_cols``
        slice), and the outer alias map. The last one is only reachable through
        an outer ``order_by`` — without one it is dead code, so a reversion of
        that binding alone would otherwise leave the suite green.
        """
        tables = _make_tables()
        query = StructuredQuery(
            from_table="orders",
            select=["customers.id", "orders.id"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            group_by=["customers.id", "orders.id"],
            top_n=TopNSpec(
                partition_by=["customers.id"],
                order_by=[OrderBySpec(col="orders.id", dir="desc")],
                n=1,
            ),
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
            limit=50,
        )
        stmt, _ = compile_structured_query(query, tables, Policy(), dialect="postgresql")
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        assert "ORDER BY anon_2.id_1 DESC" in compiled, compiled
        assert "SELECT anon_1.id, anon_1.id_1" in compiled, compiled
        # The outer ORDER BY resolves through `outer_alias_map`. Bound by name it
        # would rank the response by `customers.id` — a different row order under
        # LIMIT, with no error raised.
        assert "ORDER BY anon_1.id_1 ASC" in compiled, compiled

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

    def test_mandatory_row_filter_applies_despite_a_casefold_lower_disagreement_in_table_name(self):
        # "STRASSE".lower() == "strasse" but "straße".lower() == "straße" (unchanged) --
        # .casefold() unifies both to "strasse". `compile_mandatory_row_filters`'s
        # table match used to compare via `.lower()` against `effective_name_map`'s
        # own `.lower()`-keyed dict, so a policy configured with "straße" would
        # silently skip filtering a query against a table named "STRASSE" -- the
        # tenant-scoping filter would silently not apply (found while fixing
        # TODO.md item 150).
        metadata = sa.MetaData()
        strasse = sa.Table(
            "STRASSE",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("hausnummer", sa.String(20)),
        )
        tables = {"STRASSE": strasse}
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="straße", column="hausnummer", value="42")
            ]
        )
        query = StructuredQuery(from_table="STRASSE", select=["STRASSE.id"], limit=5)
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "42" in compiled

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

    @pytest.mark.parametrize("insertion_order", [("O", "o"), ("o", "O")])
    def test_mandatory_row_filter_does_not_create_phantom_alias_for_case_different_ref(
        self, insertion_order
    ):
        """Item 167: a case-different column ref to a joined alias (join declares
        alias="O", a select ref spells it "o.id") used to leave a SECOND,
        differently-cased `sa.Table.alias(...)` object in the `tables` dict
        schema validation builds -- `tables["O"]` from the join's own declared
        spelling, `tables["o"]` from the column ref's spelling -- both wrapping
        the same physical `orders` table but as two DISTINCT alias objects.
        `_apply_mandatory_row_filters` used to walk every key in `tables`, so a
        `mandatory_row_filter` on `orders` applied its `.where()` against BOTH
        alias objects; SQLAlchemy Core silently added the phantom "o" alias to
        the FROM clause as an unconditioned comma-join -- a real cartesian
        product (row duplication in the result set), confirmed by compiling
        this exact shape (see TODO.md item 167's own repro).

        Parametrized over BOTH `tables` dict insertion orders on purpose (a
        post-fix review, QG-167-1, caught that iterating only the query's own
        declared occurrence *names* wasn't enough: `tables[key]` was an exact
        dict index, but the FROM/JOIN loop resolves through `_table_by_name`,
        which is a case-INSENSITIVE, first-match-in-iteration-order search.
        `needed` in `_reflect_and_validate_scope` is a Python `set`, so which
        of "O"/"o" iterates first -- and therefore which object actually ends
        up in the FROM/JOIN clause -- is hash-order dependent, not guaranteed
        to be the declared spelling. The `("o", "O")` case reproduces that:
        without routing the row-filter lookup through `_table_by_name` too, the
        filter binds to an alias object ABSENT from the FROM/JOIN clause
        (recreating the phantom comma-join) while the alias actually projected
        goes completely unfiltered -- a mandatory-row-filter bypass, not just a
        cartesian product.)"""
        metadata = sa.MetaData()
        customers = sa.Table(
            "customers",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
        )
        orders = sa.Table(
            "orders",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("customer_id", sa.Integer),
            sa.Column("status", sa.String(20)),
        )
        # Mirrors exactly what `_reflect_and_validate_scope` builds for this
        # query: one entry for the join's declared alias ("O"), plus one
        # phantom entry for the column ref's differently-cased spelling ("o")
        # -- inserted in each parametrized order in turn.
        tables = {"customers": customers}
        for name in insertion_order:
            tables[name] = orders.alias(name)
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="active")
            ]
        )
        query = StructuredQuery(
            from_table="customers",
            select=["customers.id", "o.id"],
            joins=[JoinSpec(table="orders", alias="O", on=["customers.id", "O.customer_id"])],
            limit=5,
        )
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))

        # `orders` must be named exactly once in the compiled statement: once
        # via the real JOIN. A second, bare "orders" is the implicit,
        # unconditioned comma-join that multiplies rows.
        assert compiled.lower().count("orders") == 1, compiled
        assert ", orders" not in compiled.lower(), compiled
        # The filter must be applied exactly once...
        assert compiled.count("active") == 1, compiled
        # ...and specifically against whichever alias the REAL JOIN clause
        # itself rendered -- not a same-name-but-different-object alias absent
        # from FROM/JOIN. Slice up to the first comma so a reintroduced phantom
        # comma-join (item 167's original bug) can't be mistaken for the real
        # join alias by this assertion too -- it must fail on `count("orders")`
        # above, not quietly pass here by reading the phantom's own alias.
        real_join_clause = compiled.split("FROM", 1)[1].split("WHERE", 1)[0].split(",", 1)[0]
        rendered_alias = "O" if '"O"' in real_join_clause else "o"
        assert f"{rendered_alias}.status = 'active'" in compiled.replace('"', ""), compiled

    def test_mandatory_row_filter_resolves_a_case_mismatched_tables_key_without_a_raw_keyerror(
        self,
    ):
        """A `tables` key case-mismatched against the query's own from_table
        spelling (e.g. `tables={"Orders": ...}` for `from_table="orders"`) must
        still resolve through `_table_by_name` here, the same as it does for
        the FROM/JOIN construction above -- not raise a raw `KeyError` that
        would surface as an uncaught 500 instead of the usual typed
        `QueryValidationError`/successful compile."""
        metadata = sa.MetaData()
        orders = sa.Table(
            "Orders",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("status", sa.String(20)),
        )
        tables = {"Orders": orders}
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="active")
            ]
        )
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        stmt, _ = compile_structured_query(query, tables, policy)
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "active" in compiled, compiled

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


class TestCrossConnectionMandatoryRowFilters:
    """A cross-connection join's table's mandatory row filters must be drawn
    from ITS OWN resolved connection's Policy too, not just the primary
    connection's (TODO.md item 156) — the same gap item 155 closed for the
    catalog sensitivity-label trigger, applied here to
    `_apply_mandatory_row_filters`. `orders` lives on the primary connection;
    `customers` is joined in from connection `other`."""

    def _query(self) -> StructuredQuery:
        return StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )

    def _resolver(self, other_policy: Policy):
        def resolve(connection_id, principal=None):
            return None, other_policy

        return resolve

    def test_filter_configured_only_on_joined_connection_is_applied(self):
        tables = _make_tables()
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        other_policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="customers", column="country", value="US")
            ]
        )
        stmt, _ = compile_structured_query(
            query,
            tables,
            Policy(),  # the primary policy has no filter for `customers`
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(other_policy),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "customers.country = 'US'" in compiled

    def test_filter_configured_only_on_joined_connection_is_self_derived_from_the_resolver(self):
        """TODO.md item 160 finding 3 (maintainer-approved 2026-08-09): a
        caller that passes `connection_resolver`/`connection_id` but forgets
        `scope_connections` now gets it self-derived, so the joined-only
        filter still applies — unlike the pre-160 pinned regression just
        below, which passes NEITHER and correctly still doesn't filter."""
        tables = _make_tables()
        query = self._query()
        other_policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="customers", column="country", value="US")
            ]
        )
        profiles = {
            "primary": ConnectionProfile(
                id="primary",
                dialect="postgresql",
                connection_string="postgresql+asyncpg://user:pass@host/primary_db",
                join_group="grp",
            ),
            "other": ConnectionProfile(
                id="other",
                dialect="postgresql",
                connection_string="postgresql+asyncpg://user:pass@host/other_db",
                join_group="grp",
            ),
        }
        policies = {"primary": Policy(join_group="grp"), "other": other_policy}

        def resolver(connection_id, principal=None):
            return profiles[connection_id], policies[connection_id]

        stmt, _ = compile_structured_query(
            query,
            tables,
            policies["primary"],
            connection_id="primary",
            connection_resolver=resolver,
            # scope_connections intentionally omitted.
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "customers.country = 'US'" in compiled

    def test_connection_resolver_without_connection_id_is_rejected_not_silently_ignored(self):
        """Security-invariant-reviewer, 2026-08-09 (SIR-160F3-2): the
        self-derive guard requires `connection_id` too — a caller that
        passes `connection_resolver` but omits BOTH `connection_id` and
        `scope_connections` must be rejected outright, not silently
        reproduce the pre-156 primary-only behavior (the same footgun
        finding 3 closes for the "connection_id given" case)."""
        tables = _make_tables()
        query = self._query()
        with pytest.raises(QueryValidationError, match="connection_resolver"):
            compile_structured_query(
                query,
                tables,
                Policy(),
                connection_resolver=self._resolver(Policy()),
                # connection_id AND scope_connections both intentionally omitted.
            )

    def test_filter_configured_only_on_joined_connection_is_never_applied_without_the_map(self):
        """Pins the pre-156 bug: omitting `connection_id`/`scope_connections`
        (every call site before this item) resolves `mandatory_row_filters`
        from the primary policy alone, so a joined-only filter never reaches
        the compiled statement."""
        tables = _make_tables()
        query = self._query()
        stmt, _ = compile_structured_query(query, tables, Policy())
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "customers.country" not in compiled

    def test_filter_configured_only_on_primary_connection_still_applies(self):
        """Mutation guard against a REPLACE-not-union mistake: the joined
        connection has no filter at all, only the primary policy filters
        `customers` — must still apply exactly as before item 156."""
        tables = _make_tables()
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="customers", column="country", value="US")
            ]
        )
        stmt, _ = compile_structured_query(
            query,
            tables,
            policy,
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(Policy()),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "customers.country = 'US'" in compiled

    def test_both_connections_apply_their_own_different_filters(self):
        """Each connection filters a DIFFERENT table by its own rule, and
        both must land in the compiled statement — the primary connection's
        filter on `orders` and the joined connection's filter on `customers`,
        independently."""
        tables = _make_tables()
        query = self._query()
        scope_connections = {id(query): {"customers": "other"}}
        primary_policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="open")
            ]
        )
        other_policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="customers", column="country", value="US")
            ]
        )
        stmt, _ = compile_structured_query(
            query,
            tables,
            primary_policy,
            connection_id="primary",
            scope_connections=scope_connections,
            connection_resolver=self._resolver(other_policy),
        )
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "orders.status = 'open'" in compiled
        assert "customers.country = 'US'" in compiled

    def test_single_connection_filtering_unaffected_by_the_new_parameters(self):
        """Regression: a plain single-connection query must filter identically
        whether or not `connection_id`/`scope_connections` are supplied."""
        tables = _make_tables()
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="open")
            ]
        )
        stmt_old, _ = compile_structured_query(query, tables, policy)
        stmt_new, _ = compile_structured_query(
            query,
            tables,
            policy,
            connection_id="demo",
            scope_connections={id(query): {"orders": "demo"}},
        )
        compiled_old = str(stmt_old.compile(compile_kwargs={"literal_binds": True}))
        compiled_new = str(stmt_new.compile(compile_kwargs={"literal_binds": True}))
        assert compiled_old == compiled_new

    def test_single_connection_query_self_derives_as_a_no_op_when_only_the_resolver_is_given(self):
        """test-contract-reviewer, 2026-08-09 (item 160 finding-3 follow-up):
        the test above only ever calls with `{}` or with `scope_connections`+
        `connection_resolver` TOGETHER — neither exercises `connection_resolver`
        alone (`scope_connections` omitted) on a query with no cross-connection
        join. Pin that self-derivation on a single-connection query is a no-op
        that still filters identically."""
        tables = _make_tables()
        query = StructuredQuery(from_table="orders", select=["orders.id"], limit=5)
        policy = Policy(
            mandatory_row_filters=[
                MandatoryRowFilter(table="orders", column="status", value="open")
            ]
        )
        profile = ConnectionProfile(
            id="demo",
            dialect="postgresql",
            connection_string="postgresql+asyncpg://user:pass@host/demo_db",
        )

        def resolver(connection_id, principal=None):
            return profile, policy

        stmt_old, _ = compile_structured_query(query, tables, policy)
        stmt_new, _ = compile_structured_query(
            query,
            tables,
            policy,
            connection_id="demo",
            connection_resolver=resolver,
            # scope_connections intentionally omitted.
        )
        compiled_old = str(stmt_old.compile(compile_kwargs={"literal_binds": True}))
        compiled_new = str(stmt_new.compile(compile_kwargs={"literal_binds": True}))
        assert compiled_old == compiled_new


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


class TestWindowFunctions:
    """TODO.md item 101 — general window functions.

    Rendering and AST-shape rules only. The safety properties live in
    test_reference_visitor.py (every ref is visited), test_policy_validation.py
    (caps, the k-anonymity rule), tests/security (denied/masked columns), and the
    executed *values* in tests/integration/test_window_end_to_end.py plus the two
    real-database suites.
    """

    _DIALECT_COMPILERS = TestExpressionSubstrate._DIALECT_COMPILERS

    @classmethod
    def _sql(cls, payload: dict, dialect: str = "postgresql", policy: Policy = None) -> str:
        query = StructuredQuery.model_validate(payload)
        stmt, _ = compile_structured_query(
            query, _make_tables(), policy or Policy(), dialect=dialect
        )
        return str(
            stmt.compile(
                dialect=cls._DIALECT_COMPILERS[dialect],
                compile_kwargs={"literal_binds": True},
            )
        )

    @staticmethod
    def _window(**overrides) -> dict:
        item = {
            "fn": "sum",
            "arg": {"col": "orders.total_amount"},
            "over": {"order_by": [{"col": "orders.created_at"}]},
            "as": "w",
        }
        item.update(overrides)
        return {"from": "orders", "select": ["orders.id", item]}

    def test_running_total_renders_on_every_dialect(self):
        """Regression bar row 5: a running cumulative total."""
        payload = self._window(
            over={
                "order_by": [{"col": "orders.created_at"}],
                "frame": {
                    "mode": "rows",
                    "start": {"bound": "unbounded_preceding"},
                    "end": {"bound": "current_row"},
                },
            }
        )
        for dialect in ("postgresql", "mssql", "sqlite"):
            sql = self._sql(payload, dialect=dialect).lower()
            assert "sum(orders.total_amount) over (order by orders.created_at asc " in sql, dialect
            assert "rows between unbounded preceding and current row) as w" in sql, dialect

    def test_moving_average_frame_offset_renders_as_a_row_count(self):
        payload = self._window(
            fn="avg",
            over={
                "partition_by": ["orders.customer_id"],
                "order_by": [{"col": "orders.created_at"}],
                "frame": {
                    "mode": "rows",
                    "start": {"bound": "preceding", "offset": 6},
                    "end": {"bound": "current_row"},
                },
            },
        )
        sql = self._sql(payload).lower()
        assert "avg(orders.total_amount) over (partition by orders.customer_id" in sql
        assert "rows between 6 preceding and current row" in sql

    def test_omitting_the_frame_emits_no_frame_clause(self):
        """2026-07-26 Decision Log: no default frame is synthesized — the
        dialect's own SQL-standard default applies."""
        sql = self._sql(self._window()).lower()
        assert "over (order by orders.created_at asc) as w" in sql
        assert "rows between" not in sql
        assert "range between" not in sql

    def test_count_star_over_empty_window(self):
        payload = self._window(fn="count", arg=None, over={}, **{"as": "total_rows"})
        assert "count(*) OVER () AS total_rows" in self._sql(payload)

    def test_ranking_functions_render_with_partition_and_order(self):
        for fn in ("row_number", "rank", "dense_rank"):
            payload = self._window(
                fn=fn,
                arg=None,
                over={
                    "partition_by": ["orders.customer_id"],
                    "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
                },
            )
            sql = self._sql(payload).lower()
            assert (
                f"{fn}() over (partition by orders.customer_id "
                "order by orders.total_amount desc) as w" in sql
            ), fn

    def test_lag_offset_and_ntile_buckets_bind_as_typed_parameters(self):
        """A lag distance and a bucket count are caller input, so they bind as
        parameters — never `literal_column` text (plan §1 invariant 1)."""
        lag = self._window(fn="lag", offset=2)
        assert "lag(orders.total_amount, 2) OVER (ORDER BY orders.created_at ASC)" in self._sql(lag)
        ntile = self._window(fn="ntile", arg=None, buckets=4)
        assert "ntile(4) OVER (ORDER BY orders.created_at ASC)" in self._sql(ntile)

    def test_first_and_last_value_accept_a_frame(self):
        payload = self._window(
            fn="last_value",
            over={
                "order_by": [{"col": "orders.created_at"}],
                "frame": {
                    "mode": "rows",
                    "start": {"bound": "current_row"},
                    "end": {"bound": "unbounded_following"},
                },
            },
        )
        sql = self._sql(payload).lower()
        assert "last_value(orders.total_amount) over" in sql
        assert "rows between current row and unbounded following" in sql

    def test_window_over_a_computed_expression(self):
        """A window's `arg` is item 100's `Expression`, so a computed measure
        windows exactly like a bare column."""
        payload = self._window(
            arg={"op": "*", "left": {"col": "orders.total_amount"}, "right": {"literal": 2}}
        )
        assert "sum(orders.total_amount * 2) OVER" in self._sql(payload)

    def test_query_order_by_may_sort_by_a_window_alias(self):
        payload = self._window()
        payload["order_by"] = [{"col": "w", "dir": "desc"}]
        assert "ORDER BY w DESC" in self._sql(payload)

    def test_range_frame_with_a_numeric_offset_is_rejected_on_mssql_only(self):
        """T-SQL's RANGE accepts only unbounded/current-row bounds. SQLAlchemy
        compiles the offset form for every dialect regardless, so the adapter is
        the only thing standing between this and a live failure."""
        payload = self._window(
            over={
                "order_by": [{"col": "orders.id"}],
                "frame": {
                    "mode": "range",
                    "start": {"bound": "preceding", "offset": 5},
                    "end": {"bound": "current_row"},
                },
            }
        )
        assert "range between 5 preceding" in self._sql(payload, dialect="postgresql").lower()
        with pytest.raises(QueryValidationError, match="RANGE frame with a numeric offset"):
            self._sql(payload, dialect="mssql")

    def test_unbounded_range_frame_is_accepted_on_mssql(self):
        payload = self._window(
            over={
                "order_by": [{"col": "orders.id"}],
                "frame": {
                    "mode": "range",
                    "start": {"bound": "unbounded_preceding"},
                    "end": {"bound": "current_row"},
                },
            }
        )
        assert "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in self._sql(
            payload, dialect="mssql"
        )

    def test_nulls_ordering_inside_a_window_is_rejected_on_mssql(self):
        """The item-74 posture reaches inside OVER too, because the window's
        ORDER BY goes through the same `order_by_terms` adapter method."""
        payload = self._window(over={"order_by": [{"col": "orders.created_at", "nulls": "last"}]})
        assert "NULLS LAST" in self._sql(payload, dialect="postgresql")
        with pytest.raises(QueryValidationError, match="nulls first/last"):
            self._sql(payload, dialect="mssql")

    def test_window_with_top_n_ranks_over_the_projected_window(self):
        """`top_n` still materializes its own subquery; the window column rides
        out through it by output name."""
        payload = self._window()
        payload["top_n"] = {"order_by": [{"col": "orders.id", "dir": "desc"}], "n": 3}
        sql = self._sql(payload).lower()
        assert "sum(orders.total_amount) over" in sql
        assert "__rank <= 3" in sql


class TestWindowAstRules:
    """The AST-layer rules that keep one window AST from rendering fine on
    Postgres and breaking live on MSSQL, or from meaning two different things."""

    @staticmethod
    def _validate(**overrides):
        item = {"fn": "sum", "arg": {"col": "orders.total_amount"}, "over": {}, "as": "w"}
        item.update(overrides)
        return StructuredQuery.model_validate({"from": "orders", "select": [item]})

    def test_over_is_required_so_a_window_is_never_confused_with_an_aggregate(self):
        """Without `over` the payload is the plain `SUM(x)` aggregate — the two
        share {fn, arg, as}, so `over` is what makes the union unambiguous."""
        item = {"fn": "sum", "arg": {"col": "orders.total_amount"}, "as": "w"}
        query = StructuredQuery.model_validate({"from": "orders", "select": [item]})
        assert isinstance(query.select[0], AggregateSelectItem)
        assert isinstance(self._validate().select[0], WindowSelectItem)

    def test_ranking_functions_reject_an_argument(self):
        with pytest.raises(pydantic.ValidationError, match="takes no 'arg'"):
            self._validate(fn="row_number", over={"order_by": [{"col": "orders.id"}]})

    def test_value_functions_require_an_argument(self):
        with pytest.raises(pydantic.ValidationError, match="requires an 'arg'"):
            self._validate(fn="sum", arg=None)

    def test_ranking_and_offset_functions_require_order_by(self):
        for fn, extra in (
            ("row_number", {"arg": None}),
            ("rank", {"arg": None}),
            ("ntile", {"arg": None, "buckets": 4}),
            ("lag", {}),
            ("first_value", {}),
        ):
            with pytest.raises(pydantic.ValidationError, match="requires over.order_by"):
                self._validate(fn=fn, over={}, **extra)

    def test_a_frame_requires_order_by(self):
        with pytest.raises(pydantic.ValidationError, match="requires over.order_by"):
            self._validate(
                over={
                    "frame": {
                        "mode": "rows",
                        "start": {"bound": "unbounded_preceding"},
                        "end": {"bound": "current_row"},
                    }
                }
            )

    def test_a_frame_is_rejected_for_a_ranking_function(self):
        with pytest.raises(pydantic.ValidationError, match="frame is not valid"):
            self._validate(
                fn="row_number",
                arg=None,
                over={
                    "order_by": [{"col": "orders.id"}],
                    "frame": {
                        "mode": "rows",
                        "start": {"bound": "unbounded_preceding"},
                        "end": {"bound": "current_row"},
                    },
                },
            )

    def test_offset_and_buckets_are_scoped_to_their_own_functions(self):
        with pytest.raises(pydantic.ValidationError, match="only valid for lag/lead"):
            self._validate(offset=2)
        with pytest.raises(pydantic.ValidationError, match="only valid for ntile"):
            self._validate(buckets=4)
        with pytest.raises(pydantic.ValidationError, match="ntile requires 'buckets'"):
            self._validate(fn="ntile", arg=None, over={"order_by": [{"col": "orders.id"}]})

    def test_over_refs_must_be_dotted_columns_not_select_aliases(self):
        """No dialect lets an OVER clause reference a peer select alias, so the
        AST refuses the spelling outright rather than emitting invalid SQL."""
        with pytest.raises(pydantic.ValidationError, match="window partition_by"):
            self._validate(over={"partition_by": ["w"]})
        with pytest.raises(pydantic.ValidationError, match="window order_by col"):
            self._validate(over={"order_by": [{"col": "w"}]})

    def test_frame_bounds_must_be_ordered_and_sensible(self):
        rows = {"mode": "rows"}
        with pytest.raises(pydantic.ValidationError, match="cannot start at"):
            self._validate(
                over={
                    "order_by": [{"col": "orders.id"}],
                    "frame": {
                        **rows,
                        "start": {"bound": "unbounded_following"},
                        "end": {"bound": "unbounded_following"},
                    },
                }
            )
        with pytest.raises(pydantic.ValidationError, match="cannot end at"):
            self._validate(
                over={
                    "order_by": [{"col": "orders.id"}],
                    "frame": {
                        **rows,
                        "start": {"bound": "unbounded_preceding"},
                        "end": {"bound": "unbounded_preceding"},
                    },
                }
            )
        with pytest.raises(pydantic.ValidationError, match="start must not come after"):
            self._validate(
                over={
                    "order_by": [{"col": "orders.id"}],
                    "frame": {
                        **rows,
                        "start": {"bound": "following", "offset": 2},
                        "end": {"bound": "preceding", "offset": 2},
                    },
                }
            )

    def test_frame_bound_offsets_are_required_exactly_where_they_apply(self):
        with pytest.raises(pydantic.ValidationError, match="requires an offset"):
            WindowBound.model_validate({"bound": "preceding"})
        with pytest.raises(pydantic.ValidationError, match="takes no offset"):
            WindowBound.model_validate({"bound": "current_row", "offset": 1})

    def test_a_window_cannot_be_combined_with_grouping(self):
        """A window is computed over the query's rows; windowing *aggregated*
        values needs the aggregation materialized first (item 105)."""
        window = {"fn": "sum", "arg": {"col": "orders.total_amount"}, "over": {}, "as": "w"}
        with pytest.raises(pydantic.ValidationError, match="cannot be combined with group_by"):
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": ["orders.customer_id", window],
                    "group_by": ["orders.customer_id"],
                }
            )
        with pytest.raises(pydantic.ValidationError, match="cannot be combined with group_by"):
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": [{"fn": "sum", "col": "orders.total_amount"}, window],
                }
            )

    def test_top_n_cannot_rank_by_a_window_alias(self):
        """Ranking by a window's output would nest one window inside another's
        OVER clause; rejected at schema validation, where top_n refs resolve."""
        query = StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {
                        "fn": "sum",
                        "arg": {"col": "orders.total_amount"},
                        "over": {"order_by": [{"col": "orders.created_at"}]},
                        "as": "running",
                    },
                ],
                "top_n": {"order_by": [{"col": "running", "dir": "desc"}], "n": 1},
            }
        )
        with pytest.raises(QueryValidationError, match="top_n reference 'running'"):
            _validate_top_n(query, _make_tables())
