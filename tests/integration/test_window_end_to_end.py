"""End-to-end execution of general window functions (item 101).

Rendering tests prove the SQL *text* is right; these prove windows really run
and return the right **values** through the one request pipeline against a real
(SQLite) database. Ground truth is computed in Python from the same rows read
back through the SAME pipeline, so every guardrail applies on both sides and the
comparison isolates the window itself.

Row 5 of ENGINE_EXPRESSIVENESS_PLAN.md §5's canonical regression bar (a running
cumulative total) is the first test here — the wall item 101 exists to remove.
Row 3 (a 7-day moving average of DAILY order counts) is deliberately absent: it
needs a window over *aggregated* rows, which is composition over a derived table
(item 105) — see the §5 note. A moving average over row-level values, which item
101 does unblock, is covered instead.
"""

from __future__ import annotations

from itertools import accumulate

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _rows(app, body, expect=200):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json=body)
    assert resp.status_code == expect, resp.text
    return resp.json()["rows"] if expect == 200 else resp.json()


async def _orders_by_id(app):
    """Every order, ordered by id, read through the pipeline — the ground-truth
    corpus the window assertions are checked against."""
    rows = await _rows(
        app,
        {
            "from": "orders",
            "select": ["orders.id", "orders.customer_id", "orders.total_amount"],
            "order_by": [{"col": "orders.id"}],
            "limit": 100,
        },
    )
    assert len(rows) < 100, "corpus must fit under the limit or the ground truth is truncated"
    return rows


@pytest.mark.asyncio
async def test_bar_row_5_running_cumulative_total(sqlite_app):
    """Canonical bar row 5: a running total — `SUM(x) OVER (ORDER BY id ROWS
    BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)`. Impossible before item 101:
    `top_n` was the only OVER() surface and it only ranks-and-filters."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "sum",
                    "arg": {"col": "orders.total_amount"},
                    "over": {
                        "order_by": [{"col": "orders.id"}],
                        "frame": {
                            "mode": "rows",
                            "start": {"bound": "unbounded_preceding"},
                            "end": {"bound": "current_row"},
                        },
                    },
                    "as": "running_total",
                },
            ],
            "order_by": [{"col": "orders.id"}],
            "limit": 100,
        },
    )
    raw = await _orders_by_id(sqlite_app)
    expected = list(accumulate(float(r["total_amount"]) for r in raw))
    assert [float(r["running_total"]) for r in computed] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_moving_average_over_a_bounded_row_frame(sqlite_app):
    """A 3-row trailing average: the frame really bounds the window rather than
    silently averaging the whole partition."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "avg",
                    "arg": {"col": "orders.total_amount"},
                    "over": {
                        "order_by": [{"col": "orders.id"}],
                        "frame": {
                            "mode": "rows",
                            "start": {"bound": "preceding", "offset": 2},
                            "end": {"bound": "current_row"},
                        },
                    },
                    "as": "trailing_avg",
                },
            ],
            "order_by": [{"col": "orders.id"}],
            "limit": 100,
        },
    )
    amounts = [float(r["total_amount"]) for r in await _orders_by_id(sqlite_app)]
    expected = [
        sum(amounts[max(0, i - 2) : i + 1]) / len(amounts[max(0, i - 2) : i + 1])
        for i in range(len(amounts))
    ]
    assert [float(r["trailing_avg"]) for r in computed] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_row_number_within_a_partition(sqlite_app):
    """Rank-in-place: unlike `top_n`, the rank is projected and no row is
    filtered out."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                "orders.customer_id",
                {
                    "fn": "row_number",
                    "over": {
                        "partition_by": ["orders.customer_id"],
                        "order_by": [{"col": "orders.id"}],
                    },
                    "as": "nth_order",
                },
            ],
            "order_by": [{"col": "orders.id"}],
            "limit": 100,
        },
    )
    seen: dict = {}
    for row in computed:
        seen[row["customer_id"]] = seen.get(row["customer_id"], 0) + 1
        assert row["nth_order"] == seen[row["customer_id"]]
    assert len(computed) == len(await _orders_by_id(sqlite_app))


@pytest.mark.asyncio
async def test_lag_reads_the_previous_row(sqlite_app):
    """Gap analysis: each row sees its predecessor's value, and the first row's
    is NULL rather than an error."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "lag",
                    "arg": {"col": "orders.total_amount"},
                    "over": {"order_by": [{"col": "orders.id"}]},
                    "as": "previous_amount",
                },
            ],
            "order_by": [{"col": "orders.id"}],
            "limit": 100,
        },
    )
    amounts = [float(r["total_amount"]) for r in await _orders_by_id(sqlite_app)]
    assert computed[0]["previous_amount"] is None
    assert [float(r["previous_amount"]) for r in computed[1:]] == pytest.approx(amounts[:-1])


@pytest.mark.asyncio
async def test_count_star_over_the_whole_result(sqlite_app):
    """`COUNT(*) OVER ()` reports the size of the filtered row set on every row —
    the percent-of-total building block."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": ["orders.id", {"fn": "count", "over": {}, "as": "total_rows"}],
            "limit": 100,
        },
    )
    expected = len(await _orders_by_id(sqlite_app))
    assert {row["total_rows"] for row in computed} == {expected}


@pytest.mark.asyncio
async def test_ntile_splits_each_partition_into_buckets(sqlite_app):
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                {
                    "fn": "ntile",
                    "buckets": 4,
                    "over": {"order_by": [{"col": "orders.total_amount"}]},
                    "as": "quartile",
                },
            ],
            "limit": 100,
        },
    )
    assert {row["quartile"] for row in computed} == {1, 2, 3, 4}


@pytest.mark.asyncio
async def test_window_over_a_computed_expression(sqlite_app):
    """A window's `arg` is item 100's `Expression`, so a computed measure windows
    exactly like a bare column — the two pillars compose."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                "order_items.id",
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "order_items.quantity"},
                        "right": {"col": "order_items.unit_price"},
                    },
                    "over": {"partition_by": ["order_items.order_id"]},
                    "as": "order_revenue",
                },
            ],
            "order_by": [{"col": "order_items.id"}],
            "limit": 100,
        },
    )
    lines = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                "order_items.id",
                "order_items.order_id",
                "order_items.quantity",
                "order_items.unit_price",
            ],
            "order_by": [{"col": "order_items.id"}],
            "limit": 100,
        },
    )
    assert len(lines) < 100
    per_order: dict = {}
    for line in lines:
        per_order[line["order_id"]] = per_order.get(line["order_id"], 0.0) + float(
            line["quantity"]
        ) * float(line["unit_price"])
    by_id = {line["id"]: line["order_id"] for line in lines}
    for row in computed:
        assert float(row["order_revenue"]) == pytest.approx(per_order[by_id[row["id"]]])


@pytest.mark.asyncio
async def test_window_alongside_top_n_ranks_over_the_windowed_rows(sqlite_app):
    """`top_n` still materializes its own ranking subquery; the window value is
    computed over the pre-filter row set and carried out through it."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.id",
                {"fn": "count", "over": {}, "as": "total_rows"},
            ],
            "top_n": {"order_by": [{"col": "orders.id", "dir": "desc"}], "n": 3},
        },
    )
    all_rows = await _orders_by_id(sqlite_app)
    assert len(computed) == 3
    assert {row["total_rows"] for row in computed} == {len(all_rows)}
    assert [row["id"] for row in computed] == [r["id"] for r in all_rows[-3:]][::-1]


@pytest.mark.asyncio
async def test_window_with_group_by_is_rejected_before_execution(sqlite_app):
    """The AST-layer rule holds through the real transport: a window over
    aggregated rows is composition (item 105), not a silent wrong answer."""
    body = {
        "from": "orders",
        "select": [
            "orders.customer_id",
            {"fn": "sum", "arg": {"col": "orders.total_amount"}, "over": {}, "as": "w"},
        ],
        "group_by": ["orders.customer_id"],
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json=body)
    assert resp.status_code in (400, 422), resp.text
    assert "group_by" in resp.text


# --------------------------------------------------------------------------- #
# The two composition recipes docs/PRODUCT_GUIDE.md publishes for the walls item
# 101 deliberately does not remove. A recipe in the docs that nothing executes is
# a claim, not a primitive — these run both of them end-to-end so the doc cannot
# drift from what the engine actually accepts.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_documented_recipe_moving_average_over_daily_aggregates(sqlite_app):
    """Recipe 1: a window over *aggregated* rows is two round trips — the daily
    aggregate, then the rolling mean over what came back."""
    daily = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                {"col": "orders.created_at", "granularity": "day", "as": "day"},
                {"fn": "count", "col": "*", "as": "orders_that_day"},
            ],
            "group_by": ["day"],
            "order_by": [{"col": "day"}],
            "limit": 100,
        },
    )
    assert len(daily) > 1, "the aggregate must return several days or the recipe proves nothing"
    counts = [row["orders_that_day"] for row in daily]
    rolling = [
        sum(counts[max(0, i - 6) : i + 1]) / len(counts[max(0, i - 6) : i + 1])
        for i in range(len(counts))
    ]
    assert len(rolling) == len(counts) and all(value > 0 for value in rolling)


@pytest.mark.asyncio
async def test_documented_recipe_percent_of_total(sqlite_app):
    """Recipe 2, verbatim from the guide: project the row value and the window
    total side by side, then divide in the caller. Also the two engine pillars
    composing — an item-100 expression inside an item-101 window."""
    line_total = {
        "op": "*",
        "left": {"col": "order_items.quantity"},
        "right": {"col": "order_items.unit_price"},
    }
    rows = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                "order_items.id",
                "order_items.order_id",
                {"expr": line_total, "as": "line_total"},
                {
                    "fn": "sum",
                    "arg": line_total,
                    "over": {"partition_by": ["order_items.order_id"]},
                    "as": "order_total",
                },
            ],
            "order_by": [{"col": "order_items.id"}],
            "limit": 100,
        },
    )
    assert len(rows) > 1
    shares: dict = {}
    for row in rows:
        share = float(row["line_total"]) / float(row["order_total"])
        assert 0 < share <= 1
        shares[row["order_id"]] = shares.get(row["order_id"], 0.0) + share
    # Every order's line shares must sum to 1 — the property the recipe promises.
    assert len(shares) > 1, "several orders must be covered or the partition proves nothing"
    for order_id, total_share in shares.items():
        assert total_share == pytest.approx(1.0), order_id


@pytest.mark.asyncio
async def test_a_masked_column_cannot_be_a_window_argument(sqlite_app):
    """The caveat the guide states next to the recipe, enforced end-to-end: a
    masked column is a bare-projection-only value, and a window argument is a
    non-projection use — so it is refused, not silently masked in place."""
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import ColumnMask, Policy

    set_policy_store(
        PolicyStore(
            default=Policy(
                column_masks={
                    "orders": [ColumnMask(column="total_amount", kind="bucket", bucket_size=100)]
                }
            ),
            overrides={},
        )
    )
    body = {
        "from": "orders",
        "select": [
            "orders.id",
            {
                "fn": "sum",
                "arg": {"col": "orders.total_amount"},
                "over": {},
                "as": "running",
            },
        ],
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json=body)
    assert resp.status_code == 422, resp.text
    assert "masked" in resp.text
