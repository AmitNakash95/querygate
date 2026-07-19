"""Proves `list_live_tables` (schema/reflection.py) excludes Postgres's own
system catalog schemas against a real server.

Postgres's `information_schema.tables` lists `pg_catalog`/`information_schema`
system tables (`pg_type`, `pg_aggregate`, ...) alongside real base tables when
queried without a schema filter — unlike MSSQL, whose INFORMATION_SCHEMA does
not expose `sys` this way. Unfiltered, this both leaked internal database
structure into `list_tables()` for any connection without a `known_tables`
seed and made `catalog/refresh.py`'s schema-refresh scanner fail outright
(`NoSuchTableError` trying to reflect e.g. `pg_aggregate` under the wrong
schema) — found during TODO.md item 38's manual verification.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
Excluded from the default `pytest` run (see pyproject.toml's
`addopts`/`markers`).
"""

from __future__ import annotations

import pytest

from querygate.catalog.refresh import scan_connection_schema
from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.schema.reflection import list_live_tables

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"

_KNOWN_DEMO_TABLES = {"customers", "orders", "order_items", "products", "employees"}


def _use_demo_connection() -> None:
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo", dialect="postgresql", connection_string=_CONNECTION_STRING
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()


@pytest.mark.asyncio
async def test_list_live_tables_excludes_postgres_system_catalog():
    _use_demo_connection()
    tables = set(await list_live_tables("demo"))
    assert tables == _KNOWN_DEMO_TABLES
    # The exact regression this guards: pg_catalog/information_schema base
    # tables must never appear, not just "most" of them.
    assert not any(name.startswith("pg_") or name.startswith("sql_") for name in tables)


@pytest.mark.asyncio
async def test_scan_connection_schema_succeeds_without_known_tables():
    """The schema-refresh scanner (SEMANTIC_MEMORY_REFRESH_ENABLED, the
    `refresh` CLI) enumerates live tables the same way `list_tables()` does
    when a connection has no `known_tables` seed — this must not raise.
    """
    _use_demo_connection()
    snapshot = await scan_connection_schema("demo")
    assert {table.name for table in snapshot.tables} == _KNOWN_DEMO_TABLES
