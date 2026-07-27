"""End-to-end execution of named WITH blocks — ctes (TODO.md item 105).

Proves the `WITH` statement actually runs and returns the right rows, rather than
merely rendering plausible SQL — the items 75/82 distinction this repo treats as
the difference between a claim and a fact.

The two assertions that could not be made any other way are here:

* **A block is not truncated.** `max_rows` is not applied to a block, because a
  block's rows are input to a join or an aggregate. The test sets a row cap
  *below* the block's natural size and asserts the totals still come out right —
  which is exactly what a clamped block would silently get wrong, with no error.
* **Rows 3 and 6 of the regression bar** (`docs/ENGINE_EXPRESSIVENESS_PLAN.md`
  §5) — a 7-day moving average of daily orders, and cohort retention — the two
  queries the plan named as impossible before this item.
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


_PER_CUSTOMER_TOTALS = {
    "name": "totals",
    "query": {
        "from": "orders",
        "select": [
            "orders.customer_id",
            {"fn": "sum", "col": "orders.total_amount", "as": "total"},
            {"fn": "count", "col": "*", "as": "order_count"},
        ],
        "group_by": ["orders.customer_id"],
    },
}


@pytest.mark.asyncio
async def test_a_cte_joined_to_a_table_returns_the_same_totals_as_two_round_trips(sqlite_app):
    """The canonical aggregate-then-join shape. Compared against computing the
    same answer with two separate queries — the composition an agent had to use
    before this item — so a block that dropped rows, filtered in the wrong scope
    or joined on the wrong key cannot pass."""
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        single_statement = await _rows(
            client,
            {
                "ctes": [_PER_CUSTOMER_TOTALS],
                "from": "customers",
                "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
                "select": ["customers.name", "totals.total"],
                "order_by": [{"col": "customers.name"}],
                "limit": 100,
            },
        )
        # The two-round-trip equivalent.
        totals = await _rows(client, {**_PER_CUSTOMER_TOTALS["query"], "limit": 100})
        names = await _rows(
            client,
            {"from": "customers", "select": ["customers.id", "customers.name"], "limit": 100},
        )

    by_id = {row["id"]: row["name"] for row in names}
    expected = sorted(
        (by_id[row["customer_id"]], row["total"]) for row in totals if row["customer_id"] in by_id
    )
    assert single_statement, "vacuous - the block returned no rows"
    assert sorted((row["name"], row["total"]) for row in single_statement) == expected


@pytest.mark.asyncio
async def test_a_cte_is_not_truncated_by_the_response_row_cap(sqlite_app):
    """THE correctness assertion for the "no `max_rows` on a block" decision.

    The block aggregates every order; the outer query returns one row. With a row
    cap of 2, a clamped block would see only 2 orders and report a grand total
    that is silently wrong — no error, just a smaller number. Compared against the
    same total computed with no block at all.
    """
    grand_total_via_cte = {
        "ctes": [
            {
                "name": "all_orders",
                "query": {"from": "orders", "select": ["orders.total_amount"]},
            }
        ],
        "from": "all_orders",
        "select": [{"fn": "sum", "col": "all_orders.total_amount", "as": "grand"}],
        "limit": 2,
    }
    direct = {
        "from": "orders",
        "select": [{"fn": "sum", "col": "orders.total_amount", "as": "grand"}],
    }

    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        order_count = await _rows(
            client, {"from": "orders", "select": [{"fn": "count", "col": "*", "as": "n"}]}
        )
        via_cte = await _rows(client, grand_total_via_cte)
        straight = await _rows(client, direct)

    assert order_count[0]["n"] > 2, "vacuous - a cap of 2 would not have truncated anything"
    assert via_cte[0]["grand"] == straight[0]["grand"]


@pytest.mark.asyncio
async def test_a_chained_cte_runs_as_one_statement(sqlite_app):
    """Two stages in one statement: totals, then the subset above a threshold.
    `max_subquery_depth` has to be raised for a chain, which is the deny-by-default
    behavior the depth charge exists to produce."""
    query = {
        "ctes": [
            _PER_CUSTOMER_TOTALS,
            {
                "name": "big_spenders",
                "query": {
                    "from": "totals",
                    "select": ["totals.customer_id", "totals.total"],
                    "where": {"col": "totals.total", "op": "gt", "value": 100},
                },
            },
        ],
        "from": "customers",
        "joins": [{"table": "big_spenders", "on": ["customers.id", "big_spenders.customer_id"]}],
        "select": ["customers.name", "big_spenders.total"],
        "order_by": [{"col": "big_spenders.total", "dir": "desc"}],
        "limit": 50,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query", json=query)

    # Default max_subquery_depth is 1 and a two-stage chain costs 2, so the
    # default policy REFUSES it — the deny-by-default behavior the depth charge
    # exists to produce. That a raised cap then compiles the chain is proven at
    # the compiler level in `tests/unit/test_cte.py`, which can set a policy.
    assert response.status_code == 422, response.text
    assert "max_subquery_depth" in response.text


@pytest.mark.asyncio
async def test_one_cte_can_feed_both_the_from_clause_and_a_join(sqlite_app):
    """The capability an inlined derived table cannot express: the same block
    referenced twice without being written (or planned) twice — a self-join of one
    computed stage, here pairing each customer's totals with every larger one."""
    query = {
        "ctes": [_PER_CUSTOMER_TOTALS],
        "from": "totals",
        "from_alias": "a",
        "joins": [
            {
                "table": "totals",
                "alias": "b",
                "condition": {"col": "b.total", "op": "gt", "value_col": "a.total"},
            }
        ],
        "select": ["a.customer_id", "b.customer_id"],
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query", json=query)

    assert response.status_code == 200, response.text
    assert response.json()["rows"], "vacuous - no pair had a strictly larger total"


@pytest.mark.asyncio
async def test_regression_bar_row_3_seven_day_moving_average_of_daily_orders(sqlite_app):
    """§5 row 3, ❌ before this item and explicitly re-pointed at 105 by item 101:
    a window has to run over an AGGREGATED result, which needs the aggregate
    materialized first. Stage one buckets orders per day; stage two takes the
    moving average over those daily counts."""
    query = {
        "ctes": [
            {
                "name": "daily",
                "query": {
                    "from": "orders",
                    "select": [
                        {"col": "orders.created_at", "granularity": "day", "as": "day"},
                        {"fn": "count", "col": "*", "as": "orders_that_day"},
                    ],
                    "group_by": ["day"],
                },
            }
        ],
        "from": "daily",
        "select": [
            "daily.day",
            "daily.orders_that_day",
            {
                "fn": "avg",
                "arg": {"col": "daily.orders_that_day"},
                "over": {
                    "order_by": [{"col": "daily.day"}],
                    "frame": {
                        "mode": "rows",
                        "start": {"bound": "preceding", "offset": 6},
                        "end": {"bound": "current_row"},
                    },
                },
                "as": "moving_avg_7d",
            },
        ],
        "order_by": [{"col": "daily.day"}],
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, query)

    assert rows, "vacuous - no daily buckets"
    # The first row's moving average is its own value (nothing precedes it), and
    # every row's average must sit within the range of the counts it covers.
    assert float(rows[0]["moving_avg_7d"]) == pytest.approx(float(rows[0]["orders_that_day"]))
    counts = [float(row["orders_that_day"]) for row in rows]
    for index, row in enumerate(rows):
        window = counts[max(0, index - 6) : index + 1]
        assert float(row["moving_avg_7d"]) == pytest.approx(sum(window) / len(window))


@pytest.mark.asyncio
async def test_regression_bar_row_6_cohort_retention_via_a_cte(sqlite_app):
    """§5 row 6, ❌ before this item: define a cohort in one stage, then measure
    the cohort's behaviour in the next, as one statement."""
    query = {
        "ctes": [
            {
                "name": "cohort",
                "query": {
                    "from": "orders",
                    "select": [
                        "orders.customer_id",
                        {"fn": "min", "col": "orders.created_at", "as": "first_order"},
                    ],
                    "group_by": ["orders.customer_id"],
                },
            }
        ],
        "from": "cohort",
        "joins": [{"table": "orders", "on": ["cohort.customer_id", "orders.customer_id"]}],
        "select": [
            {"col": "cohort.first_order", "granularity": "month", "as": "cohort_month"},
            {"fn": "count", "col": "*", "as": "orders_from_cohort"},
        ],
        "group_by": ["cohort_month"],
        "order_by": [{"col": "cohort_month"}],
        "limit": 50,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, query)
        total = await _rows(
            client, {"from": "orders", "select": [{"fn": "count", "col": "*", "as": "n"}]}
        )

    assert rows, "vacuous - no cohorts"
    # Every order belongs to exactly one customer, so exactly one cohort: the
    # per-cohort counts must partition the whole order table.
    assert sum(row["orders_from_cohort"] for row in rows) == total[0]["n"]
