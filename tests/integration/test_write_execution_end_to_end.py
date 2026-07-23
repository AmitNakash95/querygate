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


def _enable_writes(
    *, max_affected_rows=100000, require_approval_over_rows=None, compensation_enabled=False
):
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["insert", "update", "delete"],
                    max_affected_rows=max_affected_rows,
                    require_approval_over_rows=require_approval_over_rows,
                    compensation_enabled=compensation_enabled,
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
        assert body["compensation_id"] is None  # compensation not enabled here
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


# --------------------------------------------------------------------------- #
# Bounded reversibility (item 93 phase 3a): write -> undo -> original restored #
# --------------------------------------------------------------------------- #


async def _row_ids_with_status(client, status: str) -> set:
    resp = await client.post(
        "/api/v1/demo/query",
        json={
            "from": "orders",
            "select": ["orders.id"],
            "where": {"col": "orders.status", "op": "eq", "value": status},
            "limit": 100000,
        },
    )
    return {r["id"] for r in resp.json()["rows"]}


@pytest.mark.asyncio
async def test_delete_then_undo_restores_the_rows(sqlite_app):
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        pending_ids = await _row_ids_with_status(client, "pending")
        assert pending_ids

        deleted = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert deleted.status_code == 200, deleted.text
        cid = deleted.json()["compensation_id"]
        assert cid  # a compensation record was captured
        assert await _row_ids_with_status(client, "pending") == set()  # really deleted

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text
        assert undo.json()["affected_rows"] == len(pending_ids)
        after = await _orders(client)
    assert after == before  # every deleted row is back, byte-identical


@pytest.mark.asyncio
async def test_insert_then_undo_removes_the_row(sqlite_app):
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        new_id = max(r["id"] for r in before) + 1
        inserted = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "insert",
                "table": "orders",
                "rows": [
                    {
                        "id": new_id,
                        "customer_id": 1,
                        "status": "undo-me",
                        "total_amount": 5,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            },
        )
        cid = inserted.json()["compensation_id"]
        assert await _count_status(client, "undo-me") == 1

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text
        after = await _orders(client)
    assert after == before  # the inserted row is gone


@pytest.mark.asyncio
async def test_update_then_undo_restores_old_values(sqlite_app):
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        target = before[0]["id"]

        updated = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "changed"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        cid = updated.json()["compensation_id"]
        assert await _count_status(client, "changed") == 1

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text
        after = await _orders(client)
    assert after == before  # the row's old status is restored


@pytest.mark.security
@pytest.mark.asyncio
async def test_undo_cannot_be_replayed(sqlite_app):
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        new_id = max(r["id"] for r in before) + 1
        inserted = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "insert",
                "table": "orders",
                "rows": [
                    {
                        "id": new_id,
                        "customer_id": 1,
                        "status": "once",
                        "total_amount": 5,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            },
        )
        cid = inserted.json()["compensation_id"]
        first = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert first.status_code == 200
        # Replaying the consumed id is rejected — it can't re-run or re-delete.
        second = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert second.status_code == 422
        after = await _orders(client)
    assert after == before


@pytest.mark.asyncio
async def test_over_cap_snapshot_is_skipped_not_unbounded(sqlite_app):
    # A write affecting more rows than max_compensation_rows commits, but WITHOUT
    # a compensation record — the snapshot is bounded, never an unbounded dump.
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["update"],
                    max_affected_rows=100000,
                    compensation_enabled=True,
                    max_compensation_rows=1,
                )
            ),
            overrides={},
        )
    )
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "bulk"},
                "where": {"col": "orders.id", "op": "gt", "value": 0},  # every order
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["affected_rows"] > 1
        assert resp.json()["compensation_id"] is None  # over the snapshot cap -> not captured


# --------------------------------------------------------------------------- #
# Reversibility hardening regressions (item 93 phase 3a self-review fixes)     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approved_write_undo_needs_no_token(sqlite_app, monkeypatch):
    monkeypatch.setattr(we.app_config, "approval_token_hmac_key", _KEY)
    _enable_writes(require_approval_over_rows=0, compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        target = before[0]["id"]
        # The forward UPDATE trips the gate; approve it with a token.
        from querygate.write_ast.models import UpdateStatement

        stmt = {
            "op": "update",
            "table": "orders",
            "set": {"status": "approved-change"},
            "where": {"col": "orders.id", "op": "eq", "value": target},
        }
        fp = write_fingerprint(UpdateStatement(**stmt))
        token = issue_approval_token(fingerprint=fp, approver_subject="human", key=_KEY)
        upd = await client.post(
            "/api/v1/demo/write/execute", json=stmt, headers={"X-QueryGate-Approval": token}
        )
        assert upd.status_code == 200, upd.text
        cid = upd.json()["compensation_id"]
        assert cid

        # Undo needs NO approval token even though the forward write did.
        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text
        after = await _orders(client)
    assert after == before


@pytest.mark.asyncio
async def test_update_undo_restores_only_changed_columns(sqlite_app):
    # Concern 3: undo of an UPDATE that changed `status` must NOT clobber a
    # concurrent change to `customer_id` (a column the original write never set).
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        target = (await _orders(client))[0]["id"]

        changed = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "s-changed"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        cid = changed.json()["compensation_id"]

        # A separate change to a DIFFERENT column on the same row.
        await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"customer_id": 3},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text

        row = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["orders.id", "orders.status", "orders.customer_id"],
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
    body = row.json()["rows"][0]
    assert body["status"] != "s-changed"  # status restored
    assert body["customer_id"] == 3  # the untouched-by-the-write column is preserved


@pytest.mark.security
@pytest.mark.asyncio
async def test_undo_is_atomic_and_not_consumed_on_failure(sqlite_app):
    # Concern 1: if the undo can't fully apply (here, the re-insert exceeds a
    # now-lower cap), it reverses NOTHING and the record stays usable.
    _enable_writes(max_affected_rows=100000, compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        pending = await _row_ids_with_status(client, "pending")
        assert len(pending) >= 2
        deleted = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        cid = deleted.json()["compensation_id"]
        assert await _row_ids_with_status(client, "pending") == set()

        # Tighten the cap below the number of rows the undo must re-insert.
        _enable_writes(max_affected_rows=1, compensation_enabled=True)
        failed = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert failed.status_code == 422  # over cap -> rejected
        assert await _row_ids_with_status(client, "pending") == set()  # nothing re-inserted

        # The record was NOT consumed by the failed undo — restoring the cap works.
        _enable_writes(max_affected_rows=100000, compensation_enabled=True)
        ok = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert ok.status_code == 200, ok.text
        assert await _row_ids_with_status(client, "pending") == pending


@pytest.mark.asyncio
async def test_undo_does_not_create_a_redo_record(sqlite_app):
    # Concern 6c: an undo's own inverse writes must not spawn orphaned
    # compensation records.
    from querygate.execution.compensation import get_compensation_store

    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        target = (await _orders(client))[0]["id"]
        upd = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "x"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        cid = upd.json()["compensation_id"]
        record_count_before = len(get_compensation_store()._records)
        await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
    # No new record was minted by the undo (still just the original).
    assert len(get_compensation_store()._records) == record_count_before


@pytest.mark.asyncio
async def test_auto_generated_pk_insert_is_undoable_via_returning(sqlite_app):
    # Concern 5 fixed (phase 3b): an INSERT that omits its (auto) PK captures the
    # generated key via RETURNING, so it IS undoable — undo removes exactly the
    # inserted row.
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _orders(client)
        resp = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "insert",
                "table": "orders",
                # No "id" -> SQLite assigns the rowid; captured via RETURNING.
                "rows": [
                    {
                        "customer_id": 1,
                        "status": "auto-pk",
                        "total_amount": 5,
                        "created_at": "2026-01-01T00:00:00",
                    }
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        cid = resp.json()["compensation_id"]
        assert cid is not None  # undoable — the generated key was captured
        assert await _count_status(client, "auto-pk") == 1

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 200, undo.text
        after = await _orders(client)
    assert after == before  # the auto-PK row is gone


@pytest.mark.asyncio
async def test_undo_through_redis_store_restores_full_row(sqlite_app):
    # The durable cross-replica path (phase 3b): a compensation record round-trips
    # through Redis (JSON) and the undo restores the full row — including the
    # Decimal total_amount and datetime created_at, whose types the compiler
    # re-coerces from their JSON strings.
    import fakeredis.aioredis

    from querygate.execution.compensation import init_compensation_store, reset_compensation_store
    from querygate.execution.redis_compensation import RedisCompensationStore

    init_compensation_store(RedisCompensationStore(fakeredis.aioredis.FakeRedis()))
    _enable_writes(compensation_enabled=True)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL
        ) as client:

            async def _full_row(order_id):
                r = await client.post(
                    "/api/v1/demo/query",
                    json={
                        "from": "orders",
                        "select": [
                            "orders.id",
                            "orders.status",
                            "orders.total_amount",
                            "orders.created_at",
                        ],
                        "where": {"col": "orders.id", "op": "eq", "value": order_id},
                    },
                )
                rows = r.json()["rows"]
                return rows[0] if rows else None

            target = (await _orders(client))[0]["id"]
            original = await _full_row(target)

            deleted = await client.post(
                "/api/v1/demo/write/execute",
                json={
                    "op": "delete",
                    "table": "orders",
                    "where": {"col": "orders.id", "op": "eq", "value": target},
                },
            )
            cid = deleted.json()["compensation_id"]
            assert cid and await _full_row(target) is None  # gone, record in Redis

            undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
            assert undo.status_code == 200, undo.text
            assert await _full_row(target) == original  # full row restored via Redis
    finally:
        reset_compensation_store()


@pytest.mark.security
@pytest.mark.asyncio
async def test_update_undo_refuses_on_concurrent_change_to_changed_column(sqlite_app):
    # Optimistic concurrency (phase 3b): if the very column the write changed is
    # changed again before undo, undo REFUSES rather than clobbering that change.
    _enable_writes(compensation_enabled=True)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        target = (await _orders(client))[0]["id"]

        first = await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "step-1"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
        cid = first.json()["compensation_id"]

        # A concurrent change to the SAME column after the write.
        await client.post(
            "/api/v1/demo/write/execute",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "step-2"},
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )

        undo = await client.post("/api/v1/demo/write/undo", json={"compensation_id": cid})
        assert undo.status_code == 422  # refused — would clobber the concurrent change
        assert "concurrent change" in undo.json()["detail"].lower()

        # The concurrent change is preserved, not clobbered back.
        row = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["orders.id", "orders.status"],
                "where": {"col": "orders.id", "op": "eq", "value": target},
            },
        )
    assert row.json()["rows"][0]["status"] == "step-2"


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
