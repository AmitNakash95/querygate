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
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery
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

    async def test_having_requires_group_by_or_aggregate(self, monkeypatch):
        tables = _make_tables()
        _patch_load_table(monkeypatch, tables)
        query = StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            having=[Predicate(col="orders.status", op="eq", value="completed")],
            limit=5,
        )
        with pytest.raises(ValueError, match="having requires"):
            await sv.validate_schema(query, connection_id="demo")


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
