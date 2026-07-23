"""Governed-writes dry-run preview against real SQLite (TODO.md item 93, Phase 1).

The critical property: a preview reports the affected-row count accurately and
**changes nothing** — the data is byte-identical before and after. Also proves
deny-by-default (writes off => clean policy rejection) and redaction-safety (the
returned SQL is parameterized, never literal values).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy, WritePolicy

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _enable_writes():
    set_policy_store(
        PolicyStore(
            default=Policy(
                write=WritePolicy(
                    enabled=True,
                    allowed_tables=["orders"],
                    allowed_operations=["insert", "update", "delete"],
                    max_affected_rows=100000,
                )
            ),
            overrides={},
        )
    )


async def _all_orders(client) -> list:
    resp = await client.post(
        "/api/v1/demo/query",
        json={"from": "orders", "select": ["orders.id", "orders.status"], "limit": 100000},
    )
    return sorted(resp.json()["rows"], key=lambda r: r["id"])


@pytest.mark.asyncio
async def test_update_preview_reports_count_and_changes_nothing(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        # How many orders are currently 'pending' (the count the preview should report).
        pend = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["orders.id"],
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
                "limit": 100000,
            },
        )
        expected_affected = len(pend.json()["rows"])

        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "update",
                "table": "orders",
                "set": {"status": "shipped"},
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["operation"] == "update"
        assert body["affected_rows"] == expected_affected
        assert body["executed"] is False
        assert "shipped" not in body["sql"]  # parameterized, no literal
        assert "pending" not in body["sql"]

        after = await _all_orders(client)
    # Nothing changed — the preview did not commit (or even execute the UPDATE).
    assert before == after
    assert expected_affected > 0  # the demo seed has pending orders — real matching happened


@pytest.mark.asyncio
async def test_delete_preview_changes_nothing(sqlite_app):
    _enable_writes()
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        before = await _all_orders(client)
        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.status", "op": "eq", "value": "pending"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["executed"] is False
        after = await _all_orders(client)
    assert before == after  # nothing deleted


@pytest.mark.asyncio
async def test_writes_denied_by_default_is_a_clean_rejection(sqlite_app):
    # Default policy (from conftest) has writes off — the preview must refuse.
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/write/preview",
            json={
                "op": "delete",
                "table": "orders",
                "where": {"col": "orders.id", "op": "eq", "value": 1},
            },
        )
    assert resp.status_code == 422  # PolicyViolationError -> 422
    assert "not enabled" in resp.json()["detail"].lower()
