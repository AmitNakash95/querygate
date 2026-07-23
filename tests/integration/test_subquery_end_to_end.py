"""End-to-end execution of a bounded nested subquery (TODO.md item 97).

Proves an `IN (subquery)` compiles and executes against a real (SQLite) database
and returns exactly the rows a semantically-equivalent join would — i.e. the
subquery genuinely filters, and doesn't silently degrade to an unfiltered read.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


@pytest.mark.asyncio
async def test_in_subquery_executes_and_matches_equivalent_join(sqlite_app):
    in_subquery = {
        "from": "orders",
        "select": ["orders.id"],
        "where": {
            "col": "orders.customer_id",
            "op": "in",
            "value_subquery": {
                "from": "customers",
                "select": ["customers.id"],
                "where": {"col": "customers.country", "op": "eq", "value": "US"},
            },
        },
    }
    equivalent_join = {
        "from": "orders",
        "select": ["orders.id"],
        "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
        "where": {"col": "customers.country", "op": "eq", "value": "US"},
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        sub_resp = await client.post("/api/v1/demo/query", json=in_subquery)
        join_resp = await client.post("/api/v1/demo/query", json=equivalent_join)

    assert sub_resp.status_code == 200, sub_resp.text
    assert join_resp.status_code == 200, join_resp.text
    sub_ids = {r["id"] for r in sub_resp.json()["rows"]}
    join_ids = {r["id"] for r in join_resp.json()["rows"]}
    assert sub_ids == join_ids
    assert len(sub_ids) > 0  # the demo seed has US customers with orders — real filtering happened


@pytest.mark.asyncio
async def test_not_in_subquery_is_the_complement(sqlite_app):
    not_in = {
        "from": "orders",
        "select": ["orders.id"],
        "where": {
            "col": "orders.customer_id",
            "op": "not_in",
            "value_subquery": {
                "from": "customers",
                "select": ["customers.id"],
                "where": {"col": "customers.country", "op": "eq", "value": "US"},
            },
        },
    }
    all_orders = {"from": "orders", "select": ["orders.id"], "limit": 1000}
    us_orders = {
        "from": "orders",
        "select": ["orders.id"],
        "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
        "where": {"col": "customers.country", "op": "eq", "value": "US"},
        "limit": 1000,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        not_in_ids = {
            r["id"] for r in (await client.post("/api/v1/demo/query", json=not_in)).json()["rows"]
        }
        all_ids = {
            r["id"]
            for r in (await client.post("/api/v1/demo/query", json=all_orders)).json()["rows"]
        }
        us_ids = {
            r["id"]
            for r in (await client.post("/api/v1/demo/query", json=us_orders)).json()["rows"]
        }
    # NOT IN (US customers) == all orders minus US-customer orders.
    assert not_in_ids == (all_ids - us_ids)
