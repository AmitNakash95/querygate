"""Governed write EXECUTION against a real Postgres (TODO.md item 93 phase 2).

The SQLite end-to-end test proves the write logic; this proves it against the
actual primary target — real asyncpg driver, real transaction/rollback, real
rowcount and temporal binding — the same insert→verify→update→verify→delete→
verify round-trip, plus that an over-cap write rolls back and changes nothing.

Self-cleaning: it operates on a single high-id row it owns (id 900001) and
deletes it before and after, so it never disturbs the seeded demo rows.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`). Excluded
from the default `pytest` run.
"""

from __future__ import annotations

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

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"
_TEST_ID = 900001


def _use_writable_policy(**write_overrides) -> None:
    wp = dict(
        enabled=True,
        allowed_tables=["orders"],
        allowed_operations=["insert", "update", "delete"],
        max_affected_rows=100000,
    )
    wp.update(write_overrides)
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo",
                    dialect="postgresql",
                    connection_string=_CONNECTION_STRING,
                    known_tables=["customers", "orders", "order_items"],
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(write=WritePolicy(**wp)), overrides={}))
    reset_engines()


async def _status_of(reader: StructuredQueryService, order_id: int):
    result = await reader.execute(
        StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.status"],
            where=Predicate(col="orders.id", op="eq", value=order_id),
        )
    )
    return result.rows[0]["status"] if result.rows else None


async def _delete_test_row(writer: WriteExecutionService) -> None:
    await writer.execute(
        DeleteStatement(table="orders", where=Predicate(col="orders.id", op="eq", value=_TEST_ID))
    )


@pytest.mark.asyncio
async def test_write_round_trip_commits_against_postgres():
    _use_writable_policy()
    writer = WriteExecutionService("demo")
    reader = StructuredQueryService(connection_id="demo")

    await _delete_test_row(writer)  # clear any residue from a prior failed run
    try:
        inserted = await writer.execute(
            InsertStatement(
                table="orders",
                rows=[
                    {
                        "id": _TEST_ID,
                        "customer_id": 1,
                        "status": "pg-new",
                        "total_amount": 12.50,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            )
        )
        assert inserted.affected_rows == 1
        assert await _status_of(reader, _TEST_ID) == "pg-new"

        updated = await writer.execute(
            UpdateStatement(
                table="orders",
                set={"status": "pg-handled"},
                where=Predicate(col="orders.id", op="eq", value=_TEST_ID),
            )
        )
        assert updated.affected_rows == 1
        assert await _status_of(reader, _TEST_ID) == "pg-handled"

        deleted = await writer.execute(
            DeleteStatement(
                table="orders", where=Predicate(col="orders.id", op="eq", value=_TEST_ID)
            )
        )
        assert deleted.affected_rows == 1
        assert await _status_of(reader, _TEST_ID) is None
    finally:
        await _delete_test_row(writer)


@pytest.mark.asyncio
async def test_over_cap_write_rolls_back_against_postgres():
    # A DELETE matching every seeded order, capped at 1, must abort and change
    # nothing — proving the in-transaction cap + rollback on real Postgres.
    _use_writable_policy(max_affected_rows=1)
    writer = WriteExecutionService("demo")
    reader = StructuredQueryService(connection_id="demo")

    before = await reader.execute(
        StructuredQuery(from_table="orders", select=["orders.id"], limit=100000)
    )
    with pytest.raises(Exception):
        await writer.execute(
            DeleteStatement(table="orders", where=Predicate(col="orders.id", op="gt", value=0))
        )
    after = await reader.execute(
        StructuredQuery(from_table="orders", select=["orders.id"], limit=100000)
    )
    assert before.row_count == after.row_count  # nothing deleted


@pytest.mark.asyncio
async def test_delete_then_undo_restores_rows_against_postgres():
    # Bounded reversibility (phase 3a) end-to-end on real Postgres: delete the
    # test row, then undo and confirm it is restored byte-identically.
    from querygate.execution.compensation import get_compensation_store

    _use_writable_policy(compensation_enabled=True)
    get_compensation_store().clear()
    writer = WriteExecutionService("demo")
    reader = StructuredQueryService(connection_id="demo")

    await _delete_test_row(writer)
    try:
        await writer.execute(
            InsertStatement(
                table="orders",
                rows=[
                    {
                        "id": _TEST_ID,
                        "customer_id": 1,
                        "status": "pg-undo",
                        "total_amount": 7.25,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            )
        )
        deleted = await writer.execute(
            DeleteStatement(
                table="orders", where=Predicate(col="orders.id", op="eq", value=_TEST_ID)
            )
        )
        assert deleted.compensation_id
        assert await _status_of(reader, _TEST_ID) is None  # gone

        undone = await writer.undo(deleted.compensation_id)
        assert undone.affected_rows == 1
        assert await _status_of(reader, _TEST_ID) == "pg-undo"  # restored
    finally:
        await _delete_test_row(writer)
