"""Governed write EXECUTION + undo against a real MSSQL (TODO.md item 93 phase 3b).

Proves writes work on the second supported dialect, not just Postgres/SQLite —
the compiler is dialect-agnostic Core, but only a live server verifies the
asyncpg/aioodbc differences (IDENTITY PKs, OUTPUT-vs-RETURNING key capture,
XACT_ABORT session guardrails). Self-cleaning: it inserts rows with distinctive
statuses and reverses them, so the seed is left intact.

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
from querygate.execution.compensation import reset_compensation_store
from querygate.execution.service import StructuredQueryService
from querygate.execution.write_execution import WriteExecutionService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy
from querygate.query_ast.models import Predicate, StructuredQuery
from querygate.write_ast.models import DeleteStatement, InsertStatement, UpdateStatement

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.mssql_live]

_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "localhost")
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
        allowed_operations=["insert", "update", "delete"],
        max_affected_rows=100000,
        compensation_enabled=True,
        max_compensation_rows=100,
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
    reset_compensation_store()


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
    # Omit id — MSSQL orders.id is IDENTITY; the generated key is captured via
    # OUTPUT (SQLAlchemy's RETURNING) for undo.
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
async def test_insert_then_undo_against_mssql():
    # INSERT (auto IDENTITY PK, key captured via OUTPUT) -> undo -> gone.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")
    status = "mssql-insert-undo"

    # clean any residue from a prior run
    await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.status", op="eq", value=status))
    )
    inserted = await writer.execute(_new_order(status))
    assert inserted.affected_rows == 1
    assert inserted.compensation_id  # OUTPUT-captured key => undoable
    assert await _count_status(reader, status) == 1

    undone = await writer.undo(inserted.compensation_id)
    assert undone.affected_rows == 1
    assert await _count_status(reader, status) == 0  # the inserted row is gone


@pytest.mark.asyncio
async def test_update_then_undo_against_mssql():
    # UPDATE a row's status -> undo restores the old value (changed-columns-only,
    # optimistic-concurrency) on MSSQL.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")

    # Insert a row we own, find its id, update it, undo, verify restore.
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
        assert updated.affected_rows == 1 and updated.compensation_id
        assert await _count_status(reader, "mssql-update-changed") == 1

        await writer.undo(updated.compensation_id)
        assert await _count_status(reader, "mssql-update-changed") == 0
        assert await _count_status(reader, seed_status) == 1  # old status restored
    finally:
        await writer.execute(
            DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=target))
        )


@pytest.mark.asyncio
async def test_delete_then_undo_against_mssql():
    # DELETE a row -> undo re-inserts it with its original IDENTITY key
    # (SQLAlchemy's MSSQL dialect handles SET IDENTITY_INSERT) -> restored.
    _use_writable_policy()
    writer = WriteExecutionService("mssql_demo")
    reader = StructuredQueryService(connection_id="mssql_demo")
    status = "mssql-delete-undo"

    await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.status", op="eq", value=status))
    )
    await writer.execute(_new_order(status))
    row = await reader.execute(
        StructuredQuery(
            from_table="orders",
            select=["orders.id"],
            where=Predicate(col="orders.status", op="eq", value=status),
        )
    )
    target = row.rows[0]["id"]
    try:
        deleted = await writer.execute(
            DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=target))
        )
        assert deleted.compensation_id
        assert await _count_status(reader, status) == 0  # gone

        await writer.undo(deleted.compensation_id)
        restored = await reader.execute(
            StructuredQuery(
                from_table="orders",
                select=["orders.id"],
                where=Predicate(col="orders.status", op="eq", value=status),
            )
        )
        assert restored.row_count == 1 and restored.rows[0]["id"] == target  # same key back
    finally:
        await writer.execute(
            DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=target))
        )
