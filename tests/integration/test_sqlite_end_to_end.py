"""End-to-end test against a real (SQLite) database.

No Postgres/MSSQL server is available in this environment, so this test
swaps in a real SQLite engine for the "demo" connection (real reflection,
real compiled SQL, real execution through the REST app) rather than mocking
the database layer — see `tests/integration/conftest.py`'s `sqlite_app`
fixture. Dialect-specific SQL (session guardrails, MSSQL's DATEADD/DATEDIFF
vs Postgres's date_trunc) is exercised separately in
tests/unit/test_compiler.py and connections/dialects.py — see the README's
"Current limitations" section.

Assertions here are computed from the seed data in
`examples/demo_db/schema.py` rather than hardcoded, so they keep passing if
the seed data grows — see `tests/integration/test_core_regression.py` for
the broader "won't silently break as the seed data evolves" suite.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from examples.demo_db.schema import CUSTOMERS_DATA, ORDERS_DATA

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


@pytest.mark.asyncio
async def test_execute_structured_query_end_to_end(sqlite_app):
    completed = [o for o in ORDERS_DATA if o["status"] == "completed"]
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["orders.id", "orders.status", "orders.total_amount"],
                "where": {"col": "orders.status", "op": "eq", "value": "completed"},
                "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
                "limit": len(completed),
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == len(completed)
    assert all(row["status"] == "completed" for row in body["rows"])
    amounts = [float(row["total_amount"]) for row in body["rows"]]
    assert amounts == sorted(amounts, reverse=True)


@pytest.mark.asyncio
async def test_join_end_to_end(sqlite_app):
    ada = next(c for c in CUSTOMERS_DATA if c["name"] == "Ada Lovelace")
    ada_order_count = sum(1 for o in ORDERS_DATA if o["customer_id"] == ada["id"])

    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "orders",
                "select": ["customers.name", "orders.total_amount"],
                "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
                "where": {"col": "customers.name", "op": "eq", "value": "Ada Lovelace"},
                "limit": 50,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == ada_order_count
    assert all(row["name"] == "Ada Lovelace" for row in body["rows"])


@pytest.mark.asyncio
async def test_rejects_unknown_column_end_to_end(sqlite_app):
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query", json={"from": "orders", "select": ["orders.nonexistent_column"]}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_policy_denies_table_end_to_end(sqlite_app):
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    set_policy_store(PolicyStore(default=Policy(denied_tables=["order_items"]), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query", json={"from": "order_items", "select": ["order_items.id"]}
        )
    assert resp.status_code == 422
    assert "not accessible" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_limit_is_clamped_end_to_end(sqlite_app):
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    set_policy_store(PolicyStore(default=Policy(max_limit=2), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={"from": "orders", "select": ["orders.id"], "limit": 100},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 2
    assert body["row_count"] <= 2
