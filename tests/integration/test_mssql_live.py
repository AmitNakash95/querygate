"""End-to-end tests against a real MSSQL server — see TODO.md item 2.

`connections/dialects.py` (engine URL building, connect args, session
guardrails) and the compiler's `DATEADD`/`DATEDIFF` date-bucket path were,
until this suite, covered only by unit tests that check generated SQL
*text* — never anything that actually executed against SQL Server. This
suite goes through the real REST app + real registry + real `mssql+aioodbc`
engine (no monkeypatched `get_engine`/`session_scope`, unlike
`test_sqlite_end_to_end.py`), and specifically exercises the things that
can only be verified against a live server: session guardrails
(`SET LOCK_TIMEOUT`, `XACT_ABORT`) actually taking effect, cross-database
joins via `join_group` + three-part naming, and whether a `geography`-typed
column silently breaks `describe_table`/`list_tables` reflection.

Needs two real MSSQL databases — run
`python tests/integration/setup_mssql_test_db.py` (or `make test-mssql-live`,
which runs it first) against a real MSSQL server to create and seed both:
  - `querygate_demo` — the standard demo schema (`examples/demo_db/schema.py`),
    plus a `store_locations(id, name, geo GEOGRAPHY)` table for the
    geography-column reflection test.
  - `querygate_reporting` — one extra table, `customer_regions(customer_id, region)`,
    for the cross-database join test.

Connection details are read from `QUERYGATE_TEST_MSSQL_*` env vars (see
defaults below, which match the throwaway docker setup used during
development — not meant to be real credentials).

Excluded from the default `pytest` run (see the `real_db`/`mssql_live`
markers in `pyproject.toml`'s `addopts`). Run via
`pytest -m mssql_live` once the databases above exist.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from examples.demo_db.schema import CUSTOMERS_DATA, ORDERS_DATA
from querygate.api.app import create_app
from querygate.connections.engine import reset_engines, session_scope
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.core.config import AppConfig
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mssql_live]

_BASE_URL = "http://localhost"

# Default 127.0.0.1, NOT "localhost": on macOS `localhost` resolves to ::1
# first, and docker-compose publishes this port on IPv4 only (127.0.0.1, matching
# the other services' loopback-only binding), so an ODBC connect to "localhost"
# hangs on IPv6 and dies with a Login timeout that looks like a server problem.
# CI sets QUERYGATE_TEST_MSSQL_HOST explicitly, so this default only affects
# local runs — where it is the difference between `make test-mssql-live` working
# and appearing to be broken.
_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "127.0.0.1")
_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_SA_PASSWORD = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_ODBC_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")

_DEMO_URL = f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/querygate_demo"
_REPORTING_URL = f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/querygate_reporting"

_PYODBC_CONNSTR = (
    f"DRIVER={{{_ODBC_DRIVER}}};SERVER={_HOST},{_PORT};DATABASE=querygate_demo;"
    f"UID=sa;PWD={_SA_PASSWORD};TrustServerCertificate=yes"
)


@pytest_asyncio.fixture
async def mssql_app():
    # connections/dialects.py does `from core.config import config as app_config`
    # — a direct name binding captured at import time. Reassigning
    # `core.config.config` to a new object here wouldn't propagate to that
    # already-bound reference; mutating the existing object's attributes does.
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _ODBC_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True

    set_registry(
        ConnectionRegistry(
            {
                "mssql_demo": ConnectionProfile(
                    id="mssql_demo",
                    dialect="mssql",
                    connection_string=_DEMO_URL,
                    join_group="mssql_test_group",
                    known_tables=[
                        "customers",
                        "orders",
                        "order_items",
                        "products",
                        "employees",
                        "store_locations",
                    ],
                ),
                "mssql_reporting": ConnectionProfile(
                    id="mssql_reporting",
                    dialect="mssql",
                    connection_string=_REPORTING_URL,
                    join_group="mssql_test_group",
                    known_tables=["customer_regions"],
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
    # reset_engines() just drops references without awaiting dispose() —
    # fine for the rest of the suite (mock-only, no real engines ever
    # created) but leaks real aioodbc connections here (a real, if cosmetic,
    # resource-lifecycle gap this test surfaced), so dispose explicitly first.
    from querygate.connections.engine import ENGINES

    for engine in list(ENGINES.values()):
        await engine.dispose()
    reset_engines()


@pytest.mark.asyncio
async def test_execute_structured_query_end_to_end(mssql_app):
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
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
async def test_join_end_to_end(mssql_app):
    ada = next(c for c in CUSTOMERS_DATA if c["name"] == "Ada Lovelace")
    ada_order_count = sum(1 for o in ORDERS_DATA if o["customer_id"] == ada["id"])

    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
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
async def test_date_bucket_end_to_end(mssql_app):
    """Exercises the DATEADD/DATEDIFF month-bucketing path — MSSQL doesn't
    have Postgres's date_trunc, so this is genuinely different generated SQL.
    """
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
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
    assert body["row_count"] > 0
    assert sum(row["order_count"] for row in body["rows"]) == len(ORDERS_DATA)


@pytest.mark.asyncio
async def test_top_n_end_to_end(mssql_app):
    """Exercises OVER/PARTITION BY — MSSQL requires materializing the
    aggregation as a subquery first (see sqlalchemy_compiler.py's
    `_apply_top_n`), unlike Postgres/SQLite which can reference the alias
    directly.
    """
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={
                "from": "orders",
                "select": ["orders.customer_id", "orders.total_amount"],
                "top_n": {
                    "n": 1,
                    "fn": "row_number",
                    "partition_by": ["orders.customer_id"],
                    "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
                },
                "limit": 50,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    customer_ids = [row["customer_id"] for row in body["rows"]]
    assert len(customer_ids) == len(set(customer_ids))  # exactly one row per customer


@pytest.mark.asyncio
async def test_cross_database_join_end_to_end(mssql_app):
    """The whole point of `join_group`: mssql_demo.customers joined against
    mssql_reporting.customer_regions, two different databases on the same
    server, via three-part naming (connections/engine.physical_db_name +
    schema/reflection.py's schema-qualified reflection) — resolved and
    executed entirely through mssql_demo's own engine/session.
    """
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={
                "from": "customers",
                "select": ["customers.name", "customer_regions.region"],
                "joins": [
                    {
                        "table": "customer_regions",
                        "connection": "mssql_reporting",
                        "on": ["customers.id", "customer_regions.customer_id"],
                    }
                ],
                "order_by": [{"col": "customers.id", "dir": "asc"}],
                "limit": 10,
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_count"] == 4  # customer_regions only seeds ids 1-4
    by_id = {c["id"]: c["name"] for c in CUSTOMERS_DATA}
    assert body["rows"][0]["name"] == by_id[1]
    assert body["rows"][0]["region"] == "EMEA"


@pytest.mark.asyncio
async def test_cross_database_join_rejected_outside_join_group(mssql_app):
    """A join to a connection NOT sharing the primary's join_group must be
    rejected before ever touching the database.
    """
    from querygate.connections.models import ConnectionProfile as CP
    from querygate.connections.registry import ConnectionRegistry as CR

    set_registry(
        CR(
            {
                "mssql_demo": CP(
                    id="mssql_demo",
                    dialect="mssql",
                    connection_string=_DEMO_URL,
                    join_group="group_a",
                ),
                "mssql_reporting": CP(
                    id="mssql_reporting",
                    dialect="mssql",
                    connection_string=_REPORTING_URL,
                    join_group="group_b",
                ),
            }
        )
    )
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={
                "from": "customers",
                "select": ["customers.name"],
                "joins": [
                    {
                        "table": "customer_regions",
                        "connection": "mssql_reporting",
                        "on": ["customers.id", "customer_regions.customer_id"],
                    }
                ],
                "limit": 10,
            },
        )
    assert resp.status_code == 422
    assert "join_group" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_unknown_column_end_to_end(mssql_app):
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={"from": "orders", "select": ["orders.nonexistent_column"]},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_policy_denies_table_end_to_end(mssql_app):
    set_policy_store(PolicyStore(default=Policy(denied_tables=["order_items"]), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={"from": "order_items", "select": ["order_items.id"]},
        )
    assert resp.status_code == 422
    assert "not accessible" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_limit_is_clamped_end_to_end(mssql_app):
    set_policy_store(PolicyStore(default=Policy(max_limit=2), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/mssql_demo/query",
            json={"from": "orders", "select": ["orders.id"], "limit": 100},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 2
    assert body["row_count"] <= 2


@pytest.mark.asyncio
async def test_describe_table_with_geography_column_does_not_break(mssql_app):
    """The ODBC limitation TODO.md item 2 called out: `geography`/`geometry`
    CLR user-defined types reflect to SQLAlchemy's `NullType` (confirmed
    live — the mssql dialect has no native mapping for them), so
    describe_table must not crash on a table that has one, and shouldn't
    silently report its type as the misleading literal string "NULL"
    either (see `_render_column_type` in execution/service.py).
    """
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/mssql_demo/tables/store_locations")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    columns_by_name = {c["name"]: c for c in body["columns"]}
    assert {"id", "name", "geo"} <= columns_by_name.keys()
    assert columns_by_name["geo"]["type"] != "NULL"
    assert "unsupported" in columns_by_name["geo"]["type"]


@pytest.mark.asyncio
async def test_list_tables_end_to_end(mssql_app):
    async with AsyncClient(transport=ASGITransport(app=mssql_app), base_url=_BASE_URL) as client:
        resp = await client.get("/api/v1/mssql_demo/tables")
    assert resp.status_code == 200
    assert set(resp.json()["tables"]) >= {"customers", "orders", "order_items"}


def test_session_guardrail_lock_timeout_actually_takes_effect():
    """`apply_session_guardrails` sets `SET LOCK_TIMEOUT <ms>` per session —
    this proves it's not just a config value that's plausibly correct: a
    session that already holds a row lock (never committed/rolled back)
    causes a second session's UPDATE on that same row to fail with SQL
    Server's lock-timeout error 1222, not hang indefinitely.

    Uses pyodbc directly (not QueryGate's read-only query pipeline, which
    has no UPDATE/write path to provoke a lock with) to hold the lock, and
    QueryGate's own `apply_session_guardrails` to prove the guardrail it
    sets is what SQL Server actually honors.
    """
    import asyncio

    import pyodbc  # deferred: importing this at module level breaks pytest's

    # collection phase (which imports every test module regardless of marker
    # filters) on hosts without the ODBC driver installed — see this file's
    # module docstring / TODO.md item 2's write-up.

    async def _run():
        holder = pyodbc.connect(_PYODBC_CONNSTR, autocommit=False)
        holder.cursor().execute("UPDATE customers SET name = name WHERE id = 1")
        # Row lock now held by `holder`, uncommitted, until the end of this test.

        from querygate.connections.dialects import apply_session_guardrails
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

        engine = create_async_engine(
            f"{_DEMO_URL}?driver={_ODBC_DRIVER.replace(' ', '+')}&TrustServerCertificate=Yes"
        )
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await session.begin()
                await apply_session_guardrails(
                    session, "mssql", lock_timeout_seconds=1, statement_timeout_seconds=5
                )
                import sqlalchemy as sa
                import time

                start = time.monotonic()
                with pytest.raises(Exception) as exc_info:
                    await session.execute(sa.text("UPDATE customers SET name = name WHERE id = 1"))
                elapsed = time.monotonic() - start
                assert elapsed < 4, f"lock wait took {elapsed:.1f}s, expected ~1s LOCK_TIMEOUT"
                assert "1222" in str(exc_info.value) or "timeout" in str(exc_info.value).lower()
        finally:
            holder.rollback()
            holder.close()
            await engine.dispose()

    asyncio.run(_run())


def test_query_execution_timeout_actually_cancels_a_slow_query():
    """The other half of TODO.md item 3 (Postgres's half lives in
    test_postgres_timeout.py): `build_connect_args`'s
    `{"timeout": timeout_seconds}` is pyodbc's `SQL_ATTR_QUERY_TIMEOUT` — a
    genuinely different mechanism from Postgres's per-session
    `SET LOCAL statement_timeout`, and from MSSQL's own `SET LOCK_TIMEOUT`
    tested above (that one bounds lock *acquisition* wait; this one bounds
    total query *execution* time). `WAITFOR DELAY` stands in for
    `pg_sleep()` — a genuinely long-running, already-executing query, not
    just one that fails to start.

    Unlike Postgres's guardrail (applied per-session, so a single engine
    can be reused across different `Policy.timeout_seconds` values),
    pyodbc's query timeout is baked into `connect_args` at *engine
    creation* time — so this resets the cached engine between values,
    which also means (a real, documented limitation, not fixed here) an
    MSSQL connection pool won't pick up a changed `timeout_seconds` from a
    policy hot-reload (item 5) until the engine is disposed and a fresh
    connection is opened.
    """
    import asyncio
    import time

    import sqlalchemy as sa
    from sqlalchemy.exc import DBAPIError

    async def _run():
        from querygate.core.config import config as shared_config

        shared_config.odbc_driver = _ODBC_DRIVER.replace(" ", "+")
        shared_config.db_trust_server_certificate = True

        set_registry(
            ConnectionRegistry(
                {
                    "mssql_demo": ConnectionProfile(
                        id="mssql_demo", dialect="mssql", connection_string=_DEMO_URL
                    )
                }
            )
        )
        set_policy_store(PolicyStore(default=Policy(timeout_seconds=2), overrides={}))
        reset_engines()

        start = time.monotonic()
        with pytest.raises(DBAPIError, match="[Tt]imeout"):
            async with session_scope("mssql_demo") as session:
                await session.execute(sa.text("WAITFOR DELAY '00:00:10'"))
        elapsed = time.monotonic() - start
        assert (
            elapsed < 6
        ), f"expected cancellation near 2s, took {elapsed:.1f}s (near the full 10s?)"
        assert elapsed > 1, f"cancelled suspiciously fast ({elapsed:.1f}s) for a 2s timeout"

    asyncio.run(_run())
