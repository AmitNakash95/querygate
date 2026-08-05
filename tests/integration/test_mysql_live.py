"""End-to-end tests against a real MySQL server — see TODO.md item 19.

`connections/dialects.py` (engine URL building, connect args, session
guardrails) and the compiler's MySQL-specific idioms (DATE()/SUBDATE()-based
date bucketing, GROUP_CONCAT SEPARATOR, STDDEV_SAMP/VAR_SAMP, DAYOFWEEK/WEEK
extract, DATE_ADD INTERVAL, the ON DUPLICATE KEY UPDATE upsert rejection)
were, until this suite, verified only by rendering-only unit tests and a set
of throwaway ad hoc scripts run once against a live MySQL 8.4 container
during development — never a durable regression test. This suite goes
through the real REST app + real registry + real `mysql+asyncmy` engine (no
monkeypatched `get_engine`/`session_scope`, unlike `test_sqlite_end_to_end.py`),
mirroring `test_mssql_live.py`'s shape.

Needs a real MySQL database — run `python tests/integration/setup_mysql_test_db.py`
(or `make test-mysql-live`, which runs it first) against a real MySQL server to
create and seed `querygate_demo` with the standard demo schema
(`examples/demo_db/schema.py`).

Connection details are read from `QUERYGATE_TEST_MYSQL_*` env vars (see
defaults below, which match the throwaway docker setup used during
development — not meant to be real credentials).

Excluded from the default `pytest` run (see the `real_db`/`mysql_live`
markers in `pyproject.toml`'s `addopts`). Run via `pytest -m mysql_live` once
the database above exists.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from examples.demo_db.schema import CUSTOMERS_DATA, ORDERS_DATA
from querygate.api.app import create_app
from querygate.connections.engine import ENGINES, reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mysql_live]

_BASE_URL = "http://localhost"

# Default 127.0.0.1, NOT "localhost": see test_mssql_live.py's identical
# comment — the same macOS IPv6-resolution/docker-publishes-IPv4-only
# reasoning applies to asyncmy's TCP connect.
_HOST = os.environ.get("QUERYGATE_TEST_MYSQL_HOST", "127.0.0.1")
_PORT = os.environ.get("QUERYGATE_TEST_MYSQL_PORT", "13306")
_ROOT_PASSWORD = os.environ.get("QUERYGATE_TEST_MYSQL_ROOT_PASSWORD", "QueryGate_Test_Pw1")

_DEMO_URL = f"mysql+asyncmy://root:{_ROOT_PASSWORD}@{_HOST}:{_PORT}/querygate_demo"


@pytest_asyncio.fixture
async def mysql_app():
    set_registry(
        ConnectionRegistry(
            {
                "mysql_demo": ConnectionProfile(
                    id="mysql_demo",
                    dialect="mysql",
                    connection_string=_DEMO_URL,
                    known_tables=["customers", "orders", "order_items", "products", "employees"],
                ),
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()

    settings = AppConfig(
        environment="localhost",
        mcp_enabled=False,
        audit_sink_backend="none",
    )
    app = create_app(settings)
    yield app
    # reset_engines() just drops references without awaiting dispose() — see
    # test_mssql_live.py's identical fixture teardown for why real engines
    # need explicit disposal here (a real, if cosmetic, resource-lifecycle
    # gap that suite already surfaced).
    for engine in list(ENGINES.values()):
        await engine.dispose()
    reset_engines()


@pytest.mark.asyncio
async def test_execute_structured_query_end_to_end(mysql_app):
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={
                "from": "orders",
                "select": ["orders.id", "orders.status", "orders.total_amount"],
                "where": {"col": "orders.status", "op": "eq", "value": "completed"},
                "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
                "limit": len(completed),
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == len(completed)
    assert all(row["status"] == "completed" for row in body["rows"])
    amounts = [float(row["total_amount"]) for row in body["rows"]]
    assert amounts == sorted(amounts, reverse=True)


@pytest.mark.asyncio
async def test_join_end_to_end(mysql_app):
    ada = next(c for c in CUSTOMERS_DATA if c["name"] == "Ada Lovelace")
    ada_order_count = sum(1 for o in ORDERS_DATA if o["customer_id"] == ada["id"])

    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={
                "from": "orders",
                "select": ["customers.name", "orders.total_amount"],
                "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
                "where": {"col": "customers.name", "op": "eq", "value": "Ada Lovelace"},
                "limit": 50,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == ada_order_count
    assert all(row["name"] == "Ada Lovelace" for row in body["rows"])


@pytest.mark.asyncio
async def test_date_bucket_end_to_end(mysql_app):
    """Exercises the SUBDATE/DATE()-based month-bucketing path — MySQL has no
    DATE_TRUNC, so this is genuinely different generated SQL from every other
    supported dialect."""
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={
                "from": "orders",
                "select": [
                    {"col": "orders.created_at", "granularity": "month", "as": "month"},
                    {"fn": "count", "col": "*", "as": "order_count"},
                ],
                "group_by": ["month"],
                "order_by": [{"col": "month", "dir": "asc"}],
                "limit": 100,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] >= 1
    assert all("order_count" in row for row in body["rows"])


@pytest.mark.asyncio
async def test_window_function_end_to_end(mysql_app):
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={
                "from": "orders",
                "select": [
                    "orders.id",
                    {
                        "fn": "row_number",
                        "over": {"order_by": [{"col": "orders.id", "dir": "asc"}]},
                        "as": "rn",
                    },
                ],
                "limit": 5,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rns = [row["rn"] for row in body["rows"]]
    assert rns == sorted(rns)


@pytest.mark.asyncio
async def test_extract_and_string_agg_end_to_end(mysql_app):
    """DAYOFWEEK/WEEK-based extract and GROUP_CONCAT SEPARATOR — both
    genuinely different generated SQL from every other supported dialect."""
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={
                "from": "customers",
                "select": [
                    {
                        "expr": {"extract": {"col": "customers.created_at"}, "part": "year"},
                        "as": "year",
                    },
                    {"col": "customers.name", "delimiter": ", ", "as": "names"},
                ],
                "group_by": ["year"],
                "limit": 100,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] >= 1
    assert all(isinstance(row["names"], str) and row["names"] for row in body["rows"])


@pytest.mark.asyncio
async def test_upsert_is_rejected_not_emulated(mysql_app):
    """MySQL's ON DUPLICATE KEY UPDATE can't target a specific conflict_columns
    set — accepting it would silently misrepresent which constraint fired the
    update, so it's rejected (item 74's reject-don't-emulate posture), the
    same way MSSQL's missing ON CONFLICT is."""
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True, allowed_tables=["customers"], allowed_operations=["upsert"]
                )
            ),
            overrides={},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/write/execute",
            json={
                "op": "upsert",
                "table": "customers",
                "rows": [
                    {
                        "id": 1,
                        "name": "Ada Lovelace",
                        "email": "ada@example.com",
                        "country": "UK",
                        "created_at": "2025-01-01T00:00:00",
                    }
                ],
                "conflict_columns": ["id"],
                "update_columns": ["name"],
            },
        )
    assert resp.status_code == 422, resp.text
    assert "ON DUPLICATE KEY UPDATE" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_unknown_column_end_to_end(mysql_app):
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={"from": "orders", "select": ["orders.nonexistent_column"]},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_policy_denies_table_end_to_end(mysql_app):
    set_policy_store(PolicyStore(default=Policy(denied_tables=["order_items"]), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={"from": "order_items", "select": ["order_items.id"]},
        )
    assert resp.status_code == 422
    assert "not accessible" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_limit_is_clamped_end_to_end(mysql_app):
    set_policy_store(PolicyStore(default=Policy(max_limit=2), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mysql_demo/query",
            json={"from": "orders", "select": ["orders.id"], "limit": 100},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 2
    assert body["row_count"] <= 2


@pytest.mark.asyncio
async def test_list_tables_excludes_mysql_system_schemas_end_to_end(mysql_app):
    """Regression for a real gap this item's own live testing found:
    schema/reflection.py's list_live_tables() excluded Postgres/MSSQL's
    system schemas but not MySQL's own two (`mysql`, `performance_schema`),
    so an unseeded known_tables connection would have leaked internal
    server tables (`user`, `plugin`, `threads`, ...) into list_tables()."""
    async with AsyncClient(transport=ASGITransport(app=mysql_app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/mysql_demo/tables")
    assert resp.status_code == 200
    tables = set(resp.json()["tables"])
    assert {"customers", "orders", "order_items"} <= tables
    assert not (tables & {"user", "plugin", "threads", "processlist", "innodb_table_stats"})


def test_session_guardrail_lock_timeout_actually_takes_effect():
    """`apply_session_guardrails` sets `SET SESSION innodb_lock_wait_timeout`
    per session — this proves it's not just a config value that's plausibly
    correct: a session that already holds a row lock (never committed/rolled
    back) causes a second session's UPDATE on that same row to fail with
    MySQL's lock-wait-timeout error 1205, not hang indefinitely.
    """
    import asyncio
    import time

    async def _run():
        holder_engine = create_async_engine(_DEMO_URL)
        holder = holder_engine.connect()
        await holder.start()
        await holder.execute(sa.text("SET SESSION autocommit = 0"))
        await holder.execute(sa.text("UPDATE customers SET name = name WHERE id = 1"))
        # Row lock now held by `holder`, uncommitted, until the end of this test.

        from querygate.connections.dialects import apply_session_guardrails

        engine = create_async_engine(_DEMO_URL)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.begin()
                await apply_session_guardrails(
                    session, "mysql", lock_timeout_seconds=1, statement_timeout_seconds=5
                )
                start = time.monotonic()
                with pytest.raises(Exception) as exc_info:
                    await session.execute(sa.text("UPDATE customers SET name = name WHERE id = 1"))
                elapsed = time.monotonic() - start
                assert elapsed < 4, f"lock wait took {elapsed:.1f}s, expected ~1s lock_wait_timeout"
                assert (
                    "1205" in str(exc_info.value)
                    or "lock wait timeout" in str(exc_info.value).lower()
                )
        finally:
            await holder.rollback()
            await holder.close()
            await holder_engine.dispose()
            await engine.dispose()

    asyncio.run(_run())


def test_session_guardrail_time_zone_actually_takes_effect():
    """`apply_session_guardrails` pins `SET SESSION time_zone = '+00:00'` —
    proves a TIMESTAMP column's EXTRACT/date_bucket reads UTC regardless of
    the server's configured zone, the same guarantee Postgres's TIME ZONE pin
    gives (MySQL's TIMESTAMP type, unlike DATETIME, converts to/from session
    time_zone on every read)."""
    import asyncio

    async def _run():
        from querygate.connections.dialects import apply_session_guardrails

        engine = create_async_engine(_DEMO_URL)
        try:
            async with AsyncSession(engine) as session:
                await apply_session_guardrails(
                    session, "mysql", lock_timeout_seconds=5, statement_timeout_seconds=10
                )
                result = await session.execute(sa.text("SELECT @@SESSION.time_zone"))
                assert result.scalar() == "+00:00"
        finally:
            await engine.dispose()

    asyncio.run(_run())
