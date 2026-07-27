"""End-to-end execution of EXISTS / correlated / scalar subqueries (item 106).

Rendering a plausible correlated statement is not evidence it correlates. These
execute and assert the ANSWER, which is the only thing that distinguishes a real
correlation from a subquery that quietly ignores the outer row — a failure mode
that produces a syntactically perfect query and uniformly wrong results.

Row 11 of the regression bar in `docs/ENGINE_EXPRESSIVENESS_PLAN.md` §5
("customers spending more than the overall average") is covered by
`test_regression_bar_row_11_above_the_overall_average`.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _rows(client: AsyncClient, query: dict) -> list:
    response = await client.post("/api/v1/demo/query", json=query)
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def _has_orders(op: str) -> dict:
    """Correlated EXISTS over CANCELLED orders specifically.

    The status filter is load-bearing, not decoration: every customer in the demo
    seed has orders, so an unfiltered EXISTS returns all 8 and NOT EXISTS returns
    none — a partition that a subquery ignoring its correlated outer row would
    reproduce exactly. Only 2 of 8 customers have a cancelled order, so both sides
    are non-empty and a dropped correlation changes the answer.
    """
    return {
        "op": op,
        "exists_subquery": {
            "from": "orders",
            "select": ["orders.id"],
            "correlate": ["customers.id"],
            "where": {
                "and": [
                    {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
                    {"col": "orders.status", "op": "eq", "value": "cancelled"},
                ]
            },
        },
    }


@pytest.mark.asyncio
async def test_exists_and_not_exists_partition_the_customer_table(sqlite_app):
    """The discriminating check. EXISTS and NOT EXISTS must be exact complements —
    a subquery that ignored the correlated outer row would return every customer
    for EXISTS and none for NOT EXISTS, which this cannot pass."""
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        everyone = await _rows(
            client, {"from": "customers", "select": ["customers.id"], "limit": 100}
        )
        with_orders = await _rows(
            client,
            {
                "from": "customers",
                "select": ["customers.id"],
                "where": _has_orders("exists"),
                "limit": 100,
            },
        )
        without = await _rows(
            client,
            {
                "from": "customers",
                "select": ["customers.id"],
                "where": _has_orders("not_exists"),
                "limit": 100,
            },
        )

    all_ids = {row["id"] for row in everyone}
    have = {row["id"] for row in with_orders}
    lack = {row["id"] for row in without}
    assert have | lack == all_ids
    assert have & lack == set()
    # Both sides non-empty, or the partition is vacuous and a broken correlation
    # could still pass.
    assert have, "vacuous - no customer had orders"
    assert lack, "vacuous - every customer had orders; NOT EXISTS proves nothing"


@pytest.mark.asyncio
async def test_not_exists_matches_the_left_join_anti_join_it_replaces(sqlite_app):
    """Cross-check against the shape that already worked (row 10: LEFT JOIN +
    is_null). Two independent routes to the same answer."""
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        via_not_exists = await _rows(
            client,
            {
                "from": "customers",
                "select": ["customers.id"],
                "where": _has_orders("not_exists"),
                "limit": 100,
            },
        )
        via_anti_join = await _rows(
            client,
            {
                "from": "customers",
                "select": ["customers.id"],
                "joins": [
                    {
                        "table": "orders",
                        "type": "left",
                        "condition": {
                            "and": [
                                {
                                    "col": "customers.id",
                                    "op": "eq",
                                    "value_col": "orders.customer_id",
                                },
                                {"col": "orders.status", "op": "eq", "value": "cancelled"},
                            ]
                        },
                    }
                ],
                "where": {"col": "orders.id", "op": "is_null"},
                "limit": 100,
            },
        )

    assert {r["id"] for r in via_not_exists} == {r["id"] for r in via_anti_join}
    assert via_not_exists, "vacuous - no customer lacked orders"


@pytest.mark.asyncio
async def test_regression_bar_row_11_above_the_overall_average(sqlite_app):
    """§5 row 11, ❌ before this item: a scalar subquery comparing each row against
    an aggregate over the whole table, in ONE statement rather than two round
    trips. Checked against the average computed separately."""
    above = {
        "from": "orders",
        "select": ["orders.id", "orders.total_amount"],
        "where": {
            "col": "orders.total_amount",
            "op": "gt",
            "value_subquery": {
                "from": "orders",
                "select": [{"fn": "avg", "col": "orders.total_amount", "as": "avg_amount"}],
            },
        },
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, above)
        avg = await _rows(
            client,
            {"from": "orders", "select": [{"fn": "avg", "col": "orders.total_amount", "as": "a"}]},
        )
        everything = await _rows(
            client, {"from": "orders", "select": ["orders.id", "orders.total_amount"], "limit": 100}
        )

    threshold = float(avg[0]["a"])
    expected = {r["id"] for r in everything if float(r["total_amount"]) > threshold}
    assert {r["id"] for r in rows} == expected
    assert rows, "vacuous - nothing was above average"
    assert len(rows) < len(everything), "vacuous - the filter excluded nothing"


@pytest.mark.asyncio
async def test_a_correlated_scalar_subquery_answers_per_outer_row(sqlite_app):
    """The strongest correlation check available: compare each order against ITS
    OWN customer's average, not the global one. A subquery ignoring the correlated
    ref would silently use the global average and return a different row set."""
    per_customer = {
        "from": "orders",
        "select": ["orders.id"],
        "where": {
            "col": "orders.total_amount",
            "op": "gt",
            "value_subquery": {
                "from": "orders",
                "from_alias": "peer",
                "select": [{"fn": "avg", "col": "peer.total_amount", "as": "a"}],
                "correlate": ["orders.customer_id"],
                "where": {
                    "col": "peer.customer_id",
                    "op": "eq",
                    "value_col": "orders.customer_id",
                },
            },
        },
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, per_customer)
        everything = await _rows(
            client,
            {
                "from": "orders",
                "select": ["orders.id", "orders.customer_id", "orders.total_amount"],
                "limit": 200,
            },
        )

    totals: dict = {}
    for row in everything:
        totals.setdefault(row["customer_id"], []).append(float(row["total_amount"]))
    expected = {
        row["id"]
        for row in everything
        if float(row["total_amount"])
        > sum(totals[row["customer_id"]]) / len(totals[row["customer_id"]])
    }
    assert {r["id"] for r in rows} == expected
    assert rows, "vacuous - no order beat its customer's own average"

    # And the per-customer answer must genuinely DIFFER from the global one, or
    # this test would pass against an implementation that dropped the correlation.
    global_rows = await_global = None
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        global_rows = await _rows(
            client,
            {
                "from": "orders",
                "select": ["orders.id"],
                "where": {
                    "col": "orders.total_amount",
                    "op": "gt",
                    "value_subquery": {
                        "from": "orders",
                        "select": [{"fn": "avg", "col": "orders.total_amount", "as": "a"}],
                    },
                },
                "limit": 100,
            },
        )
    assert {r["id"] for r in rows} != {
        r["id"] for r in global_rows
    }, "correlated and global averages returned the same rows — the test cannot detect a dropped correlation"
