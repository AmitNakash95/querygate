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
async def test_string_agg_end_to_end(sqlite_app):
    """Real execution proof (not just rendered SQL) that string_agg (item
    80) actually concatenates every grouped row's value — GB has 3 distinct
    customers, so this also proves it isn't silently truncating to one row.
    Concatenation order is unspecified without an ORDER BY-in-call (out of
    scope for item 80 — see StringAggSelectItem's docstring), so this
    compares the *set* of names, not an exact ordered string.
    """
    expected_names = {c["name"] for c in CUSTOMERS_DATA if c["country"] == "GB"}

    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "customers",
                "select": [
                    "customers.country",
                    {"col": "customers.name", "delimiter": ", ", "as": "names"},
                ],
                "where": {"col": "customers.country", "op": "eq", "value": "GB"},
                "group_by": ["customers.country"],
                "limit": 5,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 1
    got_names = set(body["rows"][0]["names"].split(", "))
    assert got_names == expected_names


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


@pytest.mark.asyncio
async def test_min_group_size_suppresses_small_groups_end_to_end(sqlite_app):
    """k-anonymity guardrail (item 88), executed for real: with min_group_size=2,
    a GROUP BY country aggregate returns only countries backed by >= 2 customers;
    a country with a single customer is suppressed. Computed from seed data."""
    from collections import Counter

    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    counts = Counter(c["country"] for c in CUSTOMERS_DATA)
    k = 2
    expected_visible = {country for country, n in counts.items() if n >= k}
    # Precondition: the seed data has both a suppressed and a surviving group,
    # or the test would prove nothing.
    assert any(n < k for n in counts.values())
    assert len(expected_visible) >= 1

    set_policy_store(PolicyStore(default=Policy(min_group_size=k), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "customers",
                "select": ["customers.country", {"fn": "count", "col": "*", "as": "n"}],
                "group_by": ["customers.country"],
                "limit": 100,
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert {row["country"] for row in body["rows"]} == expected_visible
    assert all(row["n"] >= k for row in body["rows"])


@pytest.mark.asyncio
async def test_min_group_size_suppresses_single_row_aggregate_end_to_end(sqlite_app):
    """A count over a filter matching exactly one customer is suppressed to zero
    rows under min_group_size=2 — the caller cannot single that person out."""
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    one = CUSTOMERS_DATA[0]
    set_policy_store(PolicyStore(default=Policy(min_group_size=2), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={
                "from": "customers",
                "select": [{"fn": "count", "col": "*", "as": "n"}],
                "where": {"col": "customers.id", "op": "eq", "value": one["id"]},
            },
        )
    assert resp.status_code == 200
    assert resp.json()["row_count"] == 0


@pytest.mark.asyncio
async def test_min_group_size_allows_large_group_aggregate_end_to_end(sqlite_app):
    """The whole-table count (>= k rows) is returned unchanged — the guardrail
    suppresses only under-k groups, it doesn't block aggregation."""
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    set_policy_store(PolicyStore(default=Policy(min_group_size=2), overrides={}))
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post(
            "/api/v1/demo/query",
            json={"from": "customers", "select": [{"fn": "count", "col": "*", "as": "n"}]},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] == 1
    assert body["rows"][0]["n"] == len(CUSTOMERS_DATA)
