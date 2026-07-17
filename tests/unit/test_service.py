"""Unit tests for StructuredQueryService — the seam every REST route and MCP
tool goes through. Real schema validation is bypassed via a patched
validate_schema; policy validation runs for real against a permissive Policy.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa

from querygate.execution import service as svc
from querygate.execution.service import (
    StructuredQueryResult,
    StructuredQueryService,
    TableDescription,
)
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery


def _company_table() -> sa.Table:
    return sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50)),
    )


@pytest.mark.asyncio
async def test_execute_returns_result():
    table = _company_table()
    query = StructuredQuery(
        from_table="customers", select=["customers.id", "customers.name"], limit=10
    )

    row = {"id": 1, "name": "Ada "}
    mock_result = MagicMock()
    mock_result.mappings.return_value.all.return_value = [row]
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)

    @asynccontextmanager
    async def _scope(*args, **kwargs):
        yield mock_session

    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", _scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.execute(query)

    assert isinstance(result, StructuredQueryResult)
    assert result.row_count == 1
    assert result.rows[0]["name"] == "Ada"  # whitespace-stripped
    assert result.limit == 10


@pytest.mark.asyncio
async def test_execute_many_partial_failure():
    query_ok = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    query_bad = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)
    ok_result = StructuredQueryResult(
        rows=[{"id": 1}], row_count=1, truncated=False, limit=5, offset=0
    )

    service = StructuredQueryService(connection_id="demo")
    with patch.object(service, "execute", AsyncMock(side_effect=[ok_result, ValueError("boom")])):
        results = await service.execute_many([query_ok, query_bad])

    assert len(results) == 2
    assert results[0].error is None
    assert results[0].row_count == 1
    assert results[1].error == "boom"


@pytest.mark.asyncio
async def test_explain_does_not_open_a_db_session():
    table = _company_table()
    query = StructuredQuery(from_table="customers", select=["customers.id"], limit=5)

    mock_scope = MagicMock(side_effect=AssertionError("explain() must not open a DB session"))
    with (
        patch.object(svc, "validate_schema", AsyncMock(return_value={"customers": table})),
        patch.object(svc, "session_scope", mock_scope),
    ):
        service = StructuredQueryService(connection_id="demo")
        result = await service.explain(query)

    mock_scope.assert_not_called()
    assert "customers" in result.sql
    assert result.tables == ["customers"]
    assert result.limit == 5


@pytest.mark.asyncio
async def test_list_tables_includes_known_tables():
    service = StructuredQueryService(connection_id="demo")
    with patch.object(svc, "get_metadata") as mock_meta:
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    # "demo" connection's known_tables (set in conftest.make_demo_registry)
    assert set(tables) == {"customers", "orders", "order_items"}


@pytest.mark.asyncio
async def test_list_tables_falls_back_to_live_query_without_known_tables():
    from querygate.connections.models import ConnectionProfile
    from querygate.connections.registry import ConnectionRegistry, set_registry

    profile = ConnectionProfile(
        id="demo", dialect="postgresql", connection_string="postgresql+asyncpg://x/y"
    )
    set_registry(ConnectionRegistry({"demo": profile}))

    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_metadata") as mock_meta,
        patch.object(svc, "list_live_tables", AsyncMock(return_value=["LiveOnlyTable"])),
    ):
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    assert tables == ["LiveOnlyTable"]


@pytest.mark.asyncio
async def test_list_tables_filters_denied_tables():
    set_policy_store(PolicyStore(default=Policy(denied_tables=["order_items"]), overrides={}))
    service = StructuredQueryService(connection_id="demo")
    with patch.object(svc, "get_metadata") as mock_meta:
        meta = MagicMock()
        meta.tables = {}
        mock_meta.return_value = meta
        tables = await service.list_tables()
    assert "order_items" not in tables
    assert "customers" in tables


@pytest.mark.asyncio
async def test_describe_table():
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(50), nullable=True),
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers")
    assert isinstance(desc, TableDescription)
    assert desc.name == "customers"
    assert {c.name for c in desc.columns} == {"id", "name"}


@pytest.mark.asyncio
async def test_describe_table_hides_denied_columns():
    table = sa.Table(
        "customers",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(200)),
    )
    set_policy_store(
        PolicyStore(default=Policy(denied_columns={"customers": ["email"]}), overrides={})
    )
    service = StructuredQueryService(connection_id="demo")
    with (
        patch.object(svc, "get_engine", return_value=MagicMock()),
        patch.object(svc, "get_table_schema", AsyncMock(return_value=table)),
    ):
        desc = await service.describe_table("customers")
    assert {c.name for c in desc.columns} == {"id"}


@pytest.mark.asyncio
async def test_describe_table_rejects_denied_table():
    set_policy_store(PolicyStore(default=Policy(denied_tables=["customers"]), overrides={}))
    service = StructuredQueryService(connection_id="demo")
    with pytest.raises(Exception, match="not accessible"):
        await service.describe_table("customers")
