"""Governed-writes gated execution against real SQLite (TODO.md item 93 Phase 2a).

Proves the write path actually commits — insert -> verify -> update -> verify ->
delete -> verify — and that the safety controls hold against a real database:
deny-by-default, the affected-row cap rolls the whole write back (no partial
mutation), and the approval gate pauses a large write until a token is supplied.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.execution.approval import issue_approval_token, write_fingerprint
from querygate.execution import write_execution as we
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"
_KEY = "write-approval-key"


def _enable_writes(*, max_affected_rows=100000, require_approval_over_rows=None):
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["insert", "update", "delete"],
                    max_affected_rows=max_affected_rows,
                    require_approval_over_rows=require_approval_over_rows,
                )
            ),
            overrides={},
        )
    )


async def _orders(client) -> list:
    resp = await client.post(
        "/api/v1/demo/query",
        json={"from": "orders", "select": ["orders.id", "orders.status"], "limit": 100000},
    )
    return sorted(resp.json()["rows"], key=lambda r: r["id"])


async def _count_status(client, status: str) -> int:
    resp = await client.post(
        "/api/v1/demo/query",
        json={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "eq", "value": status},
            "limit": 100000,
        },
    )
    return len(resp.json()["rows"])


@pytest.mark.asyncio
async def test_insert_update_delete_round_trip_commits(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        max_id = max(r["id"] for r in before)
        new_row = {
            "id": max_id + 1,
            "customer_id": 1,
            "status": "new",
            "total_amount": 10,
            "created_at": "2026-01-01T00:00:00",
        }

        # INSERT a new order, then verify it is really there.
        ins = await client.post(
            "/api/v1/demo/write/execute",
            json={"op": "insert", "table": "orders", "rows": [new_row]},
        )
        assert ins.status_code == 200, ins.text
        body = ins.json()
        assert body["operation"] == "insert" and body["table"] == "orders"
        assert body["affected_rows"] == 1 and body["executed"] is True
        assert await _count_status(client, "new") == 1

        # UPDATE it, verify the new value landed.
        upd = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "handled"},
                "where": {"col": "orders.id", "op": "eq", "value": max_id + 1},
            },
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["affected_rows"] == 1
        assert await _count_status(client, "handled") == 1
        assert await _count_status(client, "new") == 0

        # DELETE it, verify the row is gone and the rest of the table is intact.
        dele = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.id", "op": "eq", "value": max_id + 1},
            },
        )
        assert dele.status_code == 200, dele.text
        assert dele.json()["affected_rows"] == 1
        assert await _orders(client) == before  # back to the original rows


@pytest.mark.security
@pytest.mark.asyncio
async def test_over_cap_write_rolls_back_and_changes_nothing(sqlite_app):
    # A DELETE that would exceed the cap must abort the whole transaction.
    _enable_writes(max_affected_rows=1)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        pending = await _count_status(client, "pending")
        assert pending > 1  # the seed has several pending orders, so this trips the cap

        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 422  # over-cap -> QueryValidationError -> 422
        assert "cap" in resp.json()["detail"].lower()
        after = await _orders(client)
    assert before == after  # nothing was deleted — the whole write rolled back


@pytest.mark.security
@pytest.mark.asyncio
async def test_multi_row_insert_is_atomic_no_partial_write(sqlite_app):
    # A multi-row INSERT whose second row violates a constraint must leave the
    # table unchanged — no partial write escapes the single transaction.
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        max_id = max(r["id"] for r in before)

        def _row(oid, status):
            return {
                "id": oid,
                "customer_id": 1,
                "status": status,
                "total_amount": 10,
                "created_at": "2026-01-01T00:00:00",
            }

        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "insert",
                "table": "orders",
                # First row is fine; the second reuses an existing PK -> the whole
                # INSERT must fail atomically.
                "rows": [_row(max_id + 1, "ok"), _row(before[0]["id"], "dup")],
            },
        )
        # A constraint violation is the caller's fault -> a clean 422, not a 500,
        # and the response never leaks the raw driver/schema text.
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert "constraint" in detail.lower()
        assert "sqlite" not in detail.lower() and "IntegrityError" not in detail
        after = await _orders(client)
    assert before == after  # neither row landed — the transaction rolled back


@pytest.mark.security
@pytest.mark.asyncio
async def test_insert_missing_required_column_is_a_clean_4xx(sqlite_app):
    # Omitting a NOT NULL column (customer_id) is caught as a precise validation
    # error before the DB, not surfaced as an opaque 500.
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "insert",
                "table": "orders",
                "rows": [{"id": max(r["id"] for r in before) + 1, "status": "x"}],
            },
        )
        assert resp.status_code == 422, resp.text
        assert "missing required column" in resp.json()["detail"].lower()
        assert "customer_id" in resp.json()["detail"]
        after = await _orders(client)
    assert before == after


@pytest.mark.security
@pytest.mark.asyncio
async def test_denied_by_default_is_a_clean_rejection(sqlite_app):
    # conftest default policy has writes off.
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={"op": "insert", "table": "orders", "rows": [{"id": 999999, "status": "x"}]},
        )
        assert resp.status_code == 422
        after = await _orders(client)
    assert before == after


@pytest.mark.security
@pytest.mark.asyncio
async def test_approval_gate_pauses_then_admits(sqlite_app, monkeypatch):
    monkeypatch.setattr(we.app_config, "approval_token_hmac_key", _KEY)
    # Any write affecting >0 rows needs approval.
    _enable_writes(require_approval_over_rows=0)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        max_id = max(r["id"] for r in before)
        statement = {
            "op": "insert",
            "table": "orders",
            "rows": [
                {
                    "id": max_id + 1,
                    "customer_id": 1,
                    "status": "gated",
                    "total_amount": 10,
                    "created_at": "2026-01-01T00:00:00",
                }
            ],
        }

        # Without a token: 428, nothing written.
        paused = await client.post("/api/v1/demo/write/execute", json=statement)
        assert paused.status_code == 428, paused.text
        fingerprint = paused.json()["fingerprint"]
        assert await _orders(client) == before

        # A token bound to this exact write admits it.
        from querygate.write_ast.models import InsertStatement

        assert fingerprint == write_fingerprint(InsertStatement(**statement))
        token = issue_approval_token(fingerprint=fingerprint, approver_subject="human", key=_KEY)
        admitted = await client.post(
            "/api/v1/demo/write/execute",
            json=statement,
            headers={"X-QueryGate-Approval": token},
        )
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["affected_rows"] == 1
        assert await _count_status(client, "gated") == 1


@pytest.mark.security
@pytest.mark.asyncio
async def test_atomic_batch_is_all_or_nothing(sqlite_app):
    # execute_many(atomic=True): one failing write rolls the WHOLE batch back;
    # an all-valid atomic batch commits every write.
    from querygate.execution.write_execution import WriteExecutionService
    from querygate.write_ast.models import InsertStatement

    _enable_writes()
    svc = WriteExecutionService("demo")
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        max_id = max(r["id"] for r in before)

        def _ins(oid, status):
            return InsertStatement(
                table="orders",
                rows=[
                    {
                        "id": oid,
                        "customer_id": 1,
                        "status": status,
                        "total_amount": 1,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            )

        # Second write reuses an existing PK -> the whole atomic batch fails.
        failed = await svc.execute_many(
            [_ins(max_id + 1, "atom-a"), _ins(before[0]["id"], "dup")], atomic=True
        )
        assert all(item.error is not None for item in failed)  # all-or-nothing failure
        assert await _orders(client) == before  # nothing committed

        # An all-valid atomic batch commits every write.
        ok = await svc.execute_many(
            [_ins(max_id + 1, "atom-a"), _ins(max_id + 2, "atom-b")], atomic=True
        )
        assert all(item.executed for item in ok)
        assert await _count_status(client, "atom-a") == 1
        assert await _count_status(client, "atom-b") == 1
        for oid in (max_id + 1, max_id + 2):
            await client.post(
                "/api/v1/demo/write/execute",
                json={
                    "op": "delete",
                    "table": "orders",
                    "where": {"col": "orders.id", "op": "eq", "value": oid},
                },
            )
