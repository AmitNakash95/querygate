"""End-to-end execution of a window function used as an `Expression` operand
(item 125) — row 15 of the regression bar in `docs/ENGINE_EXPRESSIVENESS_PLAN.md`
§5, and the row that takes the bar to 16/16.

Rendering `amount - SUM(amount) OVER ()` is not evidence it computed anything.
The failure mode that matters here is silent and arithmetic: a window that lands
in the wrong scope, or an aggregate computed over the wrong partition, produces a
syntactically perfect statement and uniformly wrong numbers. So these execute and
assert the ANSWER against the seed data.

The comparisons are deliberately SUBTRACTION rather than the "share of total"
division the plan's row 15 describes in prose. `orders.total_amount` is
`NUMERIC(10, 2)`, and a quotient of two 2-decimal values is rounded to 2 decimals
— which would make the assertion a test of rounding rather than of the window.
Subtraction at that scale is exact on every dialect, so the numbers below are
real evidence. The AST shape under test — a window as an operand of a binary
arithmetic node — is identical either way, and the division form is covered by
`tests/security/test_window_operand_boundary.py` and the compiler tests.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from examples.demo_db.schema import ORDERS_DATA

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _rows(client: AsyncClient, query: dict) -> list:
    response = await client.post("/api/v1/demo/query", json=query)
    assert response.status_code == 200, response.text
    return response.json()["rows"]


def _num(value) -> Decimal:
    """A NUMERIC column round-trips through JSON as a string, so compare as
    Decimal rather than float — this is exact for the 2-decimal seed values."""
    return Decimal(str(value))


def _gap_query(partition_by: list) -> dict:
    over = {"partition_by": partition_by} if partition_by else {}
    return {
        "from": "orders",
        "select": [
            "orders.id",
            "orders.customer_id",
            {
                "expr": {
                    "op": "-",
                    "left": {"col": "orders.total_amount"},
                    "right": {
                        "fn": "sum",
                        "over": over,
                        "arg": {"col": "orders.total_amount"},
                    },
                },
                "as": "gap_to_total",
            },
        ],
        "limit": 100,
    }


async def test_regression_bar_row_15_each_row_against_an_aggregate_over_the_table(sqlite_app):
    """Row 15: each row compared against an aggregate over the WHOLE table, in one
    statement — previously two projected columns plus client-side arithmetic.

    Every row's gap must equal its own amount minus the grand total. A window that
    silently degraded to a per-row value would make every gap zero; one computed
    over the wrong rows would shift them all by a constant.
    """
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, _gap_query([]))

    grand_total = sum(_num(order["total_amount"]) for order in ORDERS_DATA)
    by_id = {order["id"]: _num(order["total_amount"]) for order in ORDERS_DATA}
    assert len(rows) == len(ORDERS_DATA)

    for row in rows:
        assert _num(row["gap_to_total"]) == by_id[row["id"]] - grand_total, row
    # Not all equal, or a constant would satisfy the loop above.
    assert len({row["gap_to_total"] for row in rows}) > 1


async def test_a_partitioned_window_operand_aggregates_within_its_own_partition(sqlite_app):
    """The same shape with PARTITION BY, which is where a wrong scope shows up:
    each order is compared against ITS customer's total, not the grand total. A
    window that ignored its partition would still return a number per row — the
    same number this test would reject.
    """
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows = await _rows(client, _gap_query(["orders.customer_id"]))

    per_customer: dict[int, Decimal] = {}
    for order in ORDERS_DATA:
        per_customer[order["customer_id"]] = per_customer.get(
            order["customer_id"], Decimal(0)
        ) + _num(order["total_amount"])
    by_id = {order["id"]: order for order in ORDERS_DATA}

    assert len(rows) == len(ORDERS_DATA)
    for row in rows:
        order = by_id[row["id"]]
        expected = _num(order["total_amount"]) - per_customer[order["customer_id"]]
        assert _num(row["gap_to_total"]) == expected, row

    # And the partitioned answer genuinely differs from the unpartitioned one, so
    # this cannot pass against a window that dropped its PARTITION BY.
    grand_total = sum(_num(order["total_amount"]) for order in ORDERS_DATA)
    assert any(per_customer[cid] != grand_total for cid in per_customer)
