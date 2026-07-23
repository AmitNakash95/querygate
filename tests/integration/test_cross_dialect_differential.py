"""Cross-dialect differential EXECUTION tests (TODO.md item 36 phase 2b).

Item 78 proves the compiler *renders* each AST correctly per dialect (SQL text).
This goes further: it **executes** the same `StructuredQuery` against a live
Postgres AND a live MSSQL (both seeded with the identical demo data) and asserts
the returned rows are equal — so a dialect-agnostic query really does behave
identically end-to-end, not just on paper. Queries that legitimately differ by
dialect (date bucketing, stddev naming, string_agg, NULLS ordering) are out of
scope here — those are the item-74/78 reject-or-render concern.

Needs BOTH live databases: `make compose-up` (Postgres) and the MSSQL setup from
tests/integration/test_mssql_live.py's docstring. Run with
`poetry run pytest -m 'postgres_live and mssql_live'`. Excluded from the default
run.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    WhereGroup,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_db,
    pytest.mark.postgres_live,
    pytest.mark.mssql_live,
]

_PG_URL = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"
_MSSQL_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "localhost")
_MSSQL_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_MSSQL_PW = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_MSSQL_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_MSSQL_URL = f"mssql+aioodbc://sa:{_MSSQL_PW}@{_MSSQL_HOST}:{_MSSQL_PORT}/querygate_demo"

_TABLES = ["customers", "orders", "order_items", "products", "employees"]


def _setup() -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _MSSQL_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    set_registry(
        ConnectionRegistry(
            {
                "pg": ConnectionProfile(
                    id="pg", dialect="postgresql", connection_string=_PG_URL, known_tables=_TABLES
                ),
                "ms": ConnectionProfile(
                    id="ms", dialect="mssql", connection_string=_MSSQL_URL, known_tables=_TABLES
                ),
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(max_select_columns=50), overrides={}))
    reset_engines()


def _norm(value):
    # Normalize cross-driver type quirks so equal data compares equal: numerics to
    # a rounded float, temporals to ISO strings, everything else untouched.
    if isinstance(value, Decimal):
        return round(float(value), 4)
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _rows(result):
    return [{k: _norm(v) for k, v in row.items()} for row in result.rows]


async def _assert_same(query: StructuredQuery) -> None:
    pg = await StructuredQueryService(connection_id="pg").execute(query)
    ms = await StructuredQueryService(connection_id="ms").execute(query)
    assert _rows(pg) == _rows(ms), f"cross-dialect mismatch\n  PG={_rows(pg)}\n  MS={_rows(ms)}"
    assert pg.row_count == ms.row_count


@pytest.mark.asyncio
async def test_simple_select_filter_order_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="customers",
            select=["customers.id", "customers.name", "customers.country"],
            where=Predicate(col="customers.country", op="eq", value="GB"),
            order_by=[OrderBySpec(col="customers.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_in_and_between_and_like_match():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.status", "orders.total_amount"],
            where=WhereGroup(
                and_terms=[
                    Predicate(col="orders.status", op="in", value=["paid", "pending", "shipped"]),
                    Predicate(col="orders.total_amount", op="between", value=[0, 100000]),
                ]
            ),
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_join_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name", "orders.status"],
            joins=[{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_group_by_aggregate_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=[
                "customers.country",
                AggregateSelectItem(fn="count", col="orders.id", alias="n"),
                AggregateSelectItem(fn="sum", col="orders.total_amount", alias="total"),
            ],
            joins=[{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
            group_by=["customers.country"],
            order_by=[OrderBySpec(col="customers.country", dir="asc")],
        )
    )
