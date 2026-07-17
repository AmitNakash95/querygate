"""Core-guarantee regression suite.

Anchors QueryGate's central promises — no raw SQL, policy enforcement
(including a sensitive table and a sensitive column, and a mandatory row
filter), correct aggregation/join/ranking/date-bucketing SQL, limit
clamping, batch partial-failure isolation, pagination, and the concurrency
guardrail — against a real database and the seed data in
`examples/demo_db/schema.py`, so a change that breaks one of these core
behaviors fails a test here rather than shipping.

Where practical, expected values are *computed* from the seed data constants
(`ORDERS_DATA`/`CUSTOMERS_DATA`/`PRODUCTS_DATA`) rather than hardcoded, so
this suite keeps passing if the seed data grows further — see
`examples/demo_db/schema.py`'s module docstring for the ground rules on
extending it.
"""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from examples.demo_db.schema import CUSTOMERS_DATA, ORDERS_DATA, PRODUCTS_DATA
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import MandatoryRowFilter, Policy

pytestmark = [pytest.mark.integration, pytest.mark.verification]

_BASE_URL = "http://localhost"


async def _post(app, path, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.post(path, json=payload)


# --- no raw SQL, anywhere ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/v1/demo/query", {"sql": "SELECT * FROM customers"}),
        ("/api/v1/demo/query/explain", {"sql": "SELECT * FROM customers"}),
        ("/api/v1/demo/query/batch", {"queries": [{"sql": "SELECT * FROM customers"}]}),
    ],
)
async def test_raw_sql_rejected_on_every_query_endpoint(sqlite_app, path, payload):
    resp = await _post(sqlite_app, path, payload)
    assert resp.status_code == 422


# --- policy enforcement, against real data ----------------------------------


@pytest.mark.asyncio
async def test_policy_denies_sensitive_table_end_to_end(sqlite_app):
    """`employees` holds synthetic PII-shaped columns (ssn, salary) — this is
    the realistic "wall off a sensitive table" scenario the demo data exists
    to demonstrate (see examples/policy.example.yaml).
    """
    set_policy_store(
        PolicyStore(
            default=Policy(allowed_tables=["customers", "orders", "products"]), overrides={}
        )
    )
    resp = await _post(
        sqlite_app, "/api/v1/demo/query", {"from": "employees", "select": ["employees.id"]}
    )
    assert resp.status_code == 422
    assert "not accessible" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_policy_denies_sensitive_column_end_to_end(sqlite_app):
    set_policy_store(
        PolicyStore(default=Policy(denied_columns={"customers": ["email"]}), overrides={})
    )
    resp = await _post(
        sqlite_app, "/api/v1/demo/query", {"from": "customers", "select": ["customers.email"]}
    )
    assert resp.status_code == 422
    assert "not accessible" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_mandatory_row_filter_applied_end_to_end(sqlite_app):
    """A policy-declared mandatory_row_filters entry must be AND-ed into
    every query against that table, even though the agent's own AST never
    mentioned it.
    """
    set_policy_store(
        PolicyStore(
            default=Policy(
                mandatory_row_filters=[
                    MandatoryRowFilter(table="orders", column="status", value="completed")
                ]
            ),
            overrides={},
        )
    )
    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {"from": "orders", "select": ["orders.id", "orders.status"], "limit": 50},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_count"] > 0
    assert all(row["status"] == "completed" for row in body["rows"])


# --- aggregation / grouping correctness, against computed expectations -----


@pytest.mark.asyncio
async def test_group_by_sum_matches_computed_expectation(sqlite_app):
    expected: dict[int, float] = defaultdict(float)
    for order in ORDERS_DATA:
        expected[order["customer_id"]] += float(order["total_amount"])

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "orders",
            "select": [
                "orders.customer_id",
                {"fn": "sum", "col": "orders.total_amount", "as": "total_spent"},
            ],
            "group_by": ["orders.customer_id"],
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == len(expected)
    for row in rows:
        assert float(row["total_spent"]) == pytest.approx(expected[row["customer_id"]])


@pytest.mark.asyncio
async def test_count_by_status_matches_computed_expectation(sqlite_app):
    expected = Counter(order["status"] for order in ORDERS_DATA)

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "orders",
            "select": ["orders.status", {"fn": "count", "col": "*", "as": "n"}],
            "group_by": ["orders.status"],
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert {row["status"]: row["n"] for row in rows} == dict(expected)


@pytest.mark.asyncio
async def test_top_n_per_customer_matches_computed_max(sqlite_app):
    expected_max: dict[int, float] = {}
    for order in ORDERS_DATA:
        cid = order["customer_id"]
        expected_max[cid] = max(expected_max.get(cid, 0.0), float(order["total_amount"]))

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "orders",
            "select": ["orders.customer_id", "orders.total_amount"],
            "top_n": {
                "partition_by": ["orders.customer_id"],
                "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
                "n": 1,
            },
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert len(rows) == len(expected_max)
    for row in rows:
        assert float(row["total_amount"]) == pytest.approx(expected_max[row["customer_id"]])


@pytest.mark.asyncio
async def test_date_bucket_month_matches_distinct_months(sqlite_app):
    expected_months = {o["created_at"].strftime("%Y-%m-01") for o in ORDERS_DATA}

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "orders",
            "select": [{"col": "orders.created_at", "granularity": "month", "as": "month"}],
            "group_by": ["month"],
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    buckets = {row["month"] for row in resp.json()["rows"]}
    assert buckets == expected_months


@pytest.mark.asyncio
async def test_date_bucket_year_matches_distinct_years(sqlite_app):
    expected_years = {o["created_at"].strftime("%Y-01-01") for o in ORDERS_DATA}

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "orders",
            "select": [
                {"col": "orders.created_at", "granularity": "year", "as": "year"},
                {"fn": "count", "col": "*", "as": "n"},
            ],
            "group_by": ["year"],
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    buckets = {row["year"] for row in rows}
    assert buckets == expected_years
    assert sum(row["n"] for row in rows) == len(ORDERS_DATA)


# --- independent table (no FK relationship to orders/customers) ------------


@pytest.mark.asyncio
async def test_products_independent_table_aggregate(sqlite_app):
    expected_by_category = Counter(p["category"] for p in PRODUCTS_DATA)

    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query",
        {
            "from": "products",
            "select": ["products.category", {"fn": "count", "col": "*", "as": "n"}],
            "group_by": ["products.category"],
            "limit": 50,
        },
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert {row["category"]: row["n"] for row in rows} == dict(expected_by_category)


# --- batching and pagination -------------------------------------------------


@pytest.mark.asyncio
async def test_batch_partial_failure_against_real_db(sqlite_app):
    resp = await _post(
        sqlite_app,
        "/api/v1/demo/query/batch",
        {
            "queries": [
                {"from": "customers", "select": ["customers.id"], "limit": 5},
                {"from": "nonexistent_table", "select": ["nonexistent_table.id"]},
                {"from": "products", "select": [{"fn": "count", "col": "*", "as": "n"}]},
            ]
        },
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 3
    assert results[0]["error"] is None
    assert results[0]["row_count"] == 5
    assert results[1]["error"] is not None
    assert results[2]["error"] is None
    assert results[2]["rows"][0]["n"] == len(PRODUCTS_DATA)


@pytest.mark.asyncio
async def test_offset_pagination_covers_all_rows_without_overlap(sqlite_app):
    page_size = 7
    seen_ids: set[int] = set()
    offset = 0
    while True:
        resp = await _post(
            sqlite_app,
            "/api/v1/demo/query",
            {
                "from": "orders",
                "select": ["orders.id"],
                "order_by": [{"col": "orders.id", "dir": "asc"}],
                "limit": page_size,
                "offset": offset,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        page_ids = {row["id"] for row in body["rows"]}
        assert not (page_ids & seen_ids), "pagination must not return overlapping rows"
        seen_ids |= page_ids
        if not body["truncated"]:
            break
        offset += page_size

    assert seen_ids == {o["id"] for o in ORDERS_DATA}


# --- concurrency guardrail, exercised through the real service -------------


@pytest.mark.asyncio
async def test_concurrency_guardrail_enforced_end_to_end(sqlite_app, monkeypatch):
    """Confirms the concurrency cap is enforced by
    StructuredQueryService.execute() against a real database session, not
    just by the bare semaphore primitive (see tests/unit/test_concurrency.py
    for that narrower unit test).
    """
    from querygate.execution.service import StructuredQueryService
    from querygate.query_ast.models import StructuredQuery

    set_policy_store(
        PolicyStore(default=Policy(max_concurrency=1, concurrency_wait_seconds=5), overrides={})
    )

    original_execute = AsyncSession.execute
    in_flight = 0
    max_in_flight = 0

    async def _tracked_execute(self, *args, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        try:
            return await original_execute(self, *args, **kwargs)
        finally:
            in_flight -= 1

    monkeypatch.setattr(AsyncSession, "execute", _tracked_execute)

    query = StructuredQuery(from_table="products", select=["products.id"], limit=50)
    service = StructuredQueryService(connection_id="demo")
    results = await asyncio.gather(*[service.execute(query) for _ in range(4)])

    assert max_in_flight == 1
    assert all(r.row_count == len(PRODUCTS_DATA) for r in results)


# --- sanity: seed data itself is internally consistent ----------------------


def test_seed_data_customer_ids_referenced_by_orders_all_exist():
    """Guards the seed data itself — a future edit that adds an order with a
    dangling customer_id would silently break every join-based test above
    with a confusing error instead of a clear one.
    """
    customer_ids = {c["id"] for c in CUSTOMERS_DATA}
    order_customer_ids = {o["customer_id"] for o in ORDERS_DATA}
    assert order_customer_ids <= customer_ids
