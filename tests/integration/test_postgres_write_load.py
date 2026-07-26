"""Concurrent-load verification for governed write guardrails (item 93 phase 2b).

Proves, against a real Postgres under genuine concurrency, the two write
guarantees a security reviewer cares about:

1. The affected-row cap holds under contention — many simultaneous over-cap
   writes are *all* rejected, and none partially commits (the table is
   byte-unchanged afterward).
2. Concurrent within-cap writes commit exactly once each — no lost or duplicated
   mutations when many writers run at the same time.

Deterministic (no timing probes): over-cap always rejects; the concurrent
inserts use distinct ids so there is no conflict to race on. Self-cleaning — it
owns a high-id range it deletes before and after, never touching seeded rows.

Run `make compose-up` first, then `make test-load` (or
`poetry run pytest -m load`). Excluded from the default suite.
"""

from __future__ import annotations

import asyncio

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.concurrency import in_process_limiter
from querygate.execution.service import StructuredQueryService
from querygate.execution.write_execution import WriteExecutionService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy
from querygate.query_ast.models import StructuredQuery
from querygate.write_ast.models import DeleteStatement, InsertStatement, WritePredicate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_db,
    pytest.mark.postgres_live,
    pytest.mark.load,
]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"
_ID_BASE = 900200


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
    # A high concurrency cap so the writes actually run at once (we are testing
    # the DB-level row cap under contention, not the admission queue).
    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=20, write=WritePolicy(**wp)), overrides={})
    )
    in_process_limiter().clear()
    reset_engines()


def _row(order_id: int) -> dict:
    return {
        "id": order_id,
        "customer_id": 1,
        "status": "load",
        "total_amount": 1,
        "created_at": "2026-01-01T00:00:00",
    }


async def _order_count(reader: StructuredQueryService) -> int:
    result = await reader.execute(
        StructuredQuery(from_table="orders", select=["orders.id"], limit=100000)
    )
    return result.row_count


async def _delete_id(writer: WriteExecutionService, order_id: int) -> None:
    await writer.execute(
        DeleteStatement(
            table="orders", where=WritePredicate(col="orders.id", op="eq", value=order_id)
        )
    )


@pytest.mark.asyncio
async def test_concurrent_over_cap_writes_all_reject_and_change_nothing():
    _use_writable_policy(max_affected_rows=1)  # a "delete every order" is always over-cap
    writer = WriteExecutionService("demo")
    reader = StructuredQueryService(connection_id="demo")

    before = await _order_count(reader)
    results = await asyncio.gather(
        *(
            writer.execute(
                DeleteStatement(
                    table="orders", where=WritePredicate(col="orders.id", op="gt", value=0)
                )
            )
            for _ in range(8)
        ),
        return_exceptions=True,
    )
    # Every concurrent over-cap write was rejected...
    assert all(isinstance(r, Exception) for r in results)
    # ...and not one of them partially committed.
    assert await _order_count(reader) == before


@pytest.mark.asyncio
async def test_concurrent_within_cap_inserts_all_commit_exactly():
    _use_writable_policy()
    writer = WriteExecutionService("demo")
    reader = StructuredQueryService(connection_id="demo")
    ids = list(range(_ID_BASE, _ID_BASE + 12))

    for oid in ids:  # clear any residue
        await _delete_id(writer, oid)
    try:
        before = await _order_count(reader)
        results = await asyncio.gather(
            *(writer.execute(InsertStatement(table="orders", rows=[_row(oid)])) for oid in ids)
        )
        assert all(r.affected_rows == 1 for r in results)
        # Exactly len(ids) rows appeared — none lost or duplicated under load.
        assert await _order_count(reader) == before + len(ids)
    finally:
        for oid in ids:
            await _delete_id(writer, oid)
