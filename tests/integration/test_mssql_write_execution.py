"""Governed write EXECUTION against a real MSSQL (TODO.md item 93).

Proves writes work on the second supported dialect, not just Postgres/SQLite —
the compiler is dialect-agnostic Core, but only a live server verifies the
asyncpg/aioodbc differences (IDENTITY PKs, XACT_ABORT session guardrails).
Self-cleaning: it inserts rows with distinctive statuses and removes them, so the
seed is left intact.

Needs a real MSSQL — see tests/integration/test_mssql_live.py's docstring for
setup (`python tests/integration/setup_mssql_test_db.py`), then
`poetry run pytest -m mssql_live`. Excluded from the default run.
"""

from __future__ import annotations

import os

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.execution.write_execution import WriteExecutionService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy
from querygate.query_ast.models import Predicate, StructuredQuery
from querygate.write_ast.models import DeleteStatement, InsertStatement, UpdateStatement

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mssql_live]

# 127.0.0.1, not "localhost": on macOS localhost resolves to ::1 first while
# docker-compose publishes this port on IPv4 only, so "localhost" hangs on IPv6
# and fails with a misleading Login timeout. CI sets the env var explicitly.
_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "127.0.0.1")
_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_SA_PASSWORD = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_ODBC_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_DEMO_URL = f"mssql+aioodbc://sa:{_SA_PASSWORD}@{_HOST}:{_PORT}/querygate_demo"


def _use_writable_policy(**write_overrides) -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _ODBC_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    wp = dict(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["insert", "update", "delete", "upsert"],
        max_affected_rows=100000,
    )
    wp.update(write_overrides)
    set_registry(
        ConnectionRegistry(
            {
                "mssql_demo": ConnectionProfile(
                    id="mssql_demo",
                    dialect="mssql",
                    connection_string=_DEMO_URL,
                    known_tables=["customers", "orders", "order_items"],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(write=WritePolicy(**wp)), overrides={}))
    reset_engines()


async def _count_status(reader: StructuredQueryService, status: str) -> int:
    result = await reader.execute(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="eq", value=status),
        )
    )
    return result.row_count


def _new_order(status: str) -> InsertStatement:
    # Omit id — MSSQL orders.id is IDENTITY; the server generates the key.
    return InsertStatement(
        table="orders",
        rows=[
            {
                "customer_id": 1,
                "status": status,
                "total_amount": 9.99,
                "created_at": "2026-01-01T00:00:00",
            }
        ],
    )


@pytest.mark.asyncio
async def test_insert_against_mssql():
    # INSERT (auto IDENTITY PK) commits on MSSQL.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")
    status = "mssql-insert"

    await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.status", op="eq", value=status))
    )
    try:
        inserted = await writer.execute(_new_order(status))
        assert inserted.affected_rows == 1
        assert await _count_status(reader, status) == 1
    finally:
        await writer.execute(
            DeleteStatement(
                table="orders", where=Predicate(col="orders.status", op="eq", value=status)
            )
        )


@pytest.mark.asyncio
async def test_update_against_mssql():
    # UPDATE a row's status commits on MSSQL.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")

    seed_status = "mssql-update-seed"
    await writer.execute(
        DeleteStatement(
            table="orders", where=Predicate(col="orders.status", op="eq", value=seed_status)
        )
    )
    await writer.execute(_new_order(seed_status))
    row = await reader.execute(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="eq", value=seed_status),
        )
    )
    target = row.rows[0]["id"]
    try:
        updated = await writer.execute(
            UpdateStatement(
                table="orders",
                set={"status": "mssql-update-changed"},
                where=Predicate(col="orders.id", op="eq", value=target),
            )
        )
        assert updated.affected_rows == 1
        assert await _count_status(reader, "mssql-update-changed") == 1
    finally:
        await writer.execute(
            DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=target))
        )


@pytest.mark.asyncio
async def test_delete_against_mssql():
    # DELETE a row commits on MSSQL.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")
    status = "mssql-delete"

    await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.status", op="eq", value=status))
    )
    await writer.execute(_new_order(status))
    deleted = await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.status", op="eq", value=status))
    )
    assert deleted.affected_rows == 1
    assert await _count_status(reader, status) == 0


@pytest.mark.asyncio
async def test_upsert_is_rejected_on_mssql():
    # MSSQL has no ON CONFLICT — upsert is policy-allowed but rejected at compile
    # (reject-not-emulate), with a clean, actionable error.
    from querygate.core.exceptions import QueryValidationError
    from querygate.write_ast.models import UpsertStatement

    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    with pytest.raises(QueryValidationError) as ei:
        await writer.execute(
            UpsertStatement(
                table="orders",
                rows=[
                    {
                        "id": 1,
                        "customer_id": 1,
                        "status": "x",
                        "total_amount": 1,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
                conflict_columns=["id"],
                update_columns=["status"],
            )
        )
    assert "mssql" in str(ei.value).lower()
