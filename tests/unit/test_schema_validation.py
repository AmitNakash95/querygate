"""Unit tests for schema-truth validation (table/column existence, join graph,
cross-connection join_group enforcement).
"""

from __future__ import annotations

from typing import Dict
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.auth import Principal
from querygate.core.exceptions import NotFoundError
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.query_ast.models import (
    AggregateSelectItem,
    JoinSpec,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    TopNSpec,
    WhereGroup,
)
from querygate.validation import schema_validation as sv


def _make_tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    customers = sa.Table(
        "customers",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100)),
    )
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("status", sa.String(20)),
    )
    return {"customers": customers, "orders": orders}


def _patch_load_table(monkeypatch, tables: Dict[str, sa.Table]):
    calls = []

    async def fake_load_table(connection_id, table_name, table_connection):
        calls.append((connection_id, table_name, table_connection))
        return tables[table_name]

    monkeypatch.setattr(sv, "_load_table", fake_load_table)
    return calls


@pytest.mark.asyncio
class TestValidateSchema:
    async def test_rejects_unknown_column(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(from_table="customers", select=["customers.missing"], limit=5)
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_accepts_valid_query(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(col="orders.status", op="eq", value="completed"),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_join_must_connect_to_graph(self, monkeypatch):
        tables = _make_tables()
        tables["dangling"] = sa.Table(
            "dangling", sa.MetaData(), sa.Column("id", sa.Integer), sa.Column("x", sa.Integer)
        )
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="dangling", on=["customers.id", "dangling.x"])],
            limit=5,
        )
        with pytest.raises(ValueError, match="does not connect"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_self_join_reflects_physical_table_once_and_aliases_both(self, monkeypatch):
        metadata = sa.MetaData()
        employees = sa.Table(
            "employees",
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(100)),
            sa.Column("manager_id", sa.Integer),
        )
        calls = _patch_load_table(monkeypatch, {"employees": employees})
        query = StructuredQuery(
            from_table="employees",
            from_alias="e",
            select=["e.name", "m.name"],
            joins=[
                JoinSpec(
                    table="employees",
                    alias="m",
                    on=["e.manager_id", "m.id"],
                )
            ],
            limit=5,
        )
        tables = await sv.validate_schema(query, connection_id="demo")
        assert set(tables) == {"e", "m"}
        assert tables["e"].name == "e"
        assert tables["m"].name == "m"
        assert tables["e"].element is employees
        assert tables["m"].element is employees
        assert tables["e"] is not tables["m"]
        # the physical table was only reflected once, not once per occurrence
        assert len(calls) == 1

    async def test_value_col_referencing_join_table_reflects_and_resolves(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(col="orders.status", op="neq", value_col="customers.name"),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_value_col_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="neq", value_col="orders.missing"),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_not_group_column_validated(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=WhereGroup(not_terms=Predicate(col="orders.missing", op="eq", value="x")),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_composite_join_extra_on_columns_resolved(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.status", "customers.name"]],
                )
            ],
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_composite_join_extra_on_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.missing", "customers.name"]],
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_composite_join_extra_on_referencing_third_table_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    extra_on=[["orders.id", "orders.status"]],
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="same two tables"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_predicate_col_fn_column_resolved(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
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
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders"}

    async def test_predicate_col_fn_unknown_column_rejected(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "orders.missing"}]},
                op="eq",
                value="active",
            ),
            limit=5,
        )
        with pytest.raises(ValueError, match="not found"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_predicate_col_fn_referencing_join_table_reflects_it(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[JoinSpec(table="customers", on=["orders.customer_id", "customers.id"])],
            where=Predicate(
                col_fn={"fn": "lower", "args": [{"col": "customers.name"}]},
                op="eq",
                value="ada",
            ),
            limit=5,
        )
        loaded = await sv.validate_schema(query, connection_id="demo")
        assert set(loaded) == {"orders", "customers"}

    async def test_having_requires_group_by_or_aggregate(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            having=Predicate(col="orders.status", op="eq", value="completed"),
            limit=5,
        )
        with pytest.raises(ValueError, match="having requires"):
            await sv.validate_schema(query, connection_id="demo")

    async def test_having_allowed_with_string_agg_and_no_group_by(self, monkeypatch):
        """A select-only string_agg (no group_by, no AggregateSelectItem)
        must count as an aggregate for the having-requires-aggregate rule —
        this only passes if StringAggSelectItem is recognized alongside
        AggregateSelectItem in _validate_group_by's has_aggregate check."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "delimiter": ", ", "as": "statuses"},
            ],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None

    async def test_having_allowed_with_array_agg_and_no_group_by(self, monkeypatch):
        """A select-only array_agg (no group_by, no AggregateSelectItem)
        must count as an aggregate for the having-requires-aggregate rule —
        this only passes if ArrayAggSelectItem is recognized alongside
        AggregateSelectItem/StringAggSelectItem in _validate_group_by's
        has_aggregate check."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "as": "statuses"},
            ],
            having=Predicate(col="statuses", op="neq", value=""),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None

    async def test_having_allowed_with_percentile_cont_and_no_group_by(self, monkeypatch):
        """A select-only percentile_cont (no group_by, no
        AggregateSelectItem) must count as an aggregate for the
        having-requires-aggregate rule — this only passes if
        PercentileContSelectItem is recognized in the shared
        _AGGREGATE_SELECT_ITEM_TYPES tuple _validate_group_by's
        has_aggregate check uses."""
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=[
                {"col": "orders.status", "fraction": 0.5, "as": "median"},
            ],
            having=Predicate(col="median", op="gt", value=0),
            limit=5,
        )
        result = await sv.validate_schema(query, connection_id="demo")
        assert result is not None


@pytest.mark.asyncio
class TestCrossConnectionJoins:
    def _two_connections(self, group_a="group-a", group_b="group-a"):
        primary = ConnectionProfile(
            id="primary",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/primary_db",
            join_group=group_a,
        )
        other = ConnectionProfile(
            id="other",
            dialect="mssql",
            connection_string="mssql+aioodbc://user:pass@host/other_db",
            join_group=group_b,
        )
        set_registry(ConnectionRegistry({"primary": primary, "other": other}))
        set_policy_store(PolicyStore(default=Policy(), overrides={}))

    async def test_same_join_group_allowed(self, monkeypatch):
        self._two_connections(group_a="shared", group_b="shared")
        tables = _make_tables()
        calls = _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
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
        await sv.validate_schema(query, connection_id="primary")
        # _load_table is always invoked through the primary connection's engine
        # (connection_id); table_connection records which connection's physical
        # database each table actually belongs to.
        engine_connection_ids = {conn for conn, _, _ in calls}
        assert engine_connection_ids == {"primary"}
        table_connection_map = {name: table_conn for _, name, table_conn in calls}
        assert table_connection_map["orders"] == "primary"
        assert table_connection_map["customers"] == "other"

    async def test_different_join_group_rejected(self, monkeypatch):
        self._two_connections(group_a="group-a", group_b="group-b")
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )
        with pytest.raises(ValueError, match="cross-connection"):
            await sv.validate_schema(query, connection_id="primary")

    async def test_join_group_uses_per_principal_policy(self, monkeypatch):
        self._two_connections(group_a="shared", group_b="shared")
        set_policy_store(
            PolicyStore.from_dict(
                {
                    "default": {},
                    "principals": {
                        "agent-a": {
                            "primary": {"join_group": "agent-a-primary"},
                            "other": {"join_group": "agent-a-other"},
                        }
                    },
                }
            )
        )
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )

        # Connection-level fallback says both connections share a join_group,
        # but this principal's policy splits them, so the principal-specific
        # view must reject the cross-connection join.
        await sv.validate_schema(query, connection_id="primary")
        with pytest.raises(ValueError, match="cross-connection"):
            await sv.validate_schema(
                query,
                connection_id="primary",
                principal=Principal(subject="agent-a"),
            )

    async def test_cross_connection_join_cannot_reach_principal_hidden_connection(
        self, monkeypatch
    ):
        self._two_connections(group_a="shared", group_b="shared")
        set_policy_store(
            PolicyStore.from_dict(
                {
                    "default": {"enabled": True},
                    "principals": {"agent-a": {"other": {"enabled": False}}},
                }
            )
        )
        _patch_load_table(monkeypatch, _make_tables())
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            joins=[
                JoinSpec(
                    table="customers",
                    on=["orders.customer_id", "customers.id"],
                    connection="other",
                )
            ],
            limit=5,
        )

        with pytest.raises(NotFoundError, match="Unknown connection: 'other'"):
            await sv.validate_schema(
                query,
                connection_id="primary",
                principal=Principal(subject="agent-a"),
            )


def test_aggregate_default_alias_matches_the_compilers_for_an_unaliased_column_aggregate():
    """`top_n` resolves its refs against the names `_aggregate_alias` returns,
    while the compiler labels the column with its OWN default-alias logic. The
    two must agree, or a valid top_n over an unaliased aggregate is rejected.

    Regression: when item 100 normalized `col` into `arg`, `_aggregate_alias`
    still read `item.col` — now `None` for a column aggregate — and raised
    `TypeError` instead of returning `sum_total_amount`.
    """
    item = AggregateSelectItem(fn="sum", col="orders.total_amount")
    assert item.col is None and item.arg is not None  # normalized to `arg`
    assert sv._aggregate_alias(item) == "sum_total_amount"
    assert sv._aggregate_alias(AggregateSelectItem(fn="count", col="*")) == "count_all"

    # And the compiler agrees, which is the property that actually matters.
    metadata = sa.MetaData()
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_id", sa.Integer),
        sa.Column("total_amount", sa.Numeric(10, 2)),
    )
    query = StructuredQuery(
        from_table="orders",
        select=["orders.customer_id", item],
        group_by=["orders.customer_id"],
        top_n=TopNSpec(
            partition_by=[], order_by=[OrderBySpec(col="sum_total_amount", dir="desc")], n=1
        ),
    )
    sv._validate_top_n(query, {"orders": orders})  # no raise
    stmt, _ = compile_structured_query(query, {"orders": orders}, Policy())
    assert "sum_total_amount" in str(stmt.compile(compile_kwargs={"literal_binds": True}))
