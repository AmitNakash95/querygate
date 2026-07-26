"""End-to-end execution of the bounded scalar Expression substrate (item 100).

Rendering tests prove the SQL *text* is right; these prove the substrate really
runs and returns the right **values** against a real (SQLite) database through
the one request pipeline. Each test computes its ground truth from a second
query through the SAME pipeline, so every guardrail (limits, min-group, masking)
applies identically on both sides and the comparison isolates the expression.

Rows 1, 2 and 7 of ENGINE_EXPRESSIVENESS_PLAN.md §5's canonical regression bar
are the first three tests here — they are the queries the plan named as the
concrete walls item 100 exists to remove.

The plan's other Phase 1 acceptance case — a type mismatch surfacing as a clean
typed error rather than a leaked driver error — is NOT here: SQLite is too
permissive to produce one (`'text' * 2` evaluates to 0 rather than failing), so
asserting it here would pass vacuously. It is unit-tested deterministically in
`tests/unit/test_service.py` against a raised driver error instead.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _rows(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


@pytest.mark.asyncio
async def test_bar_row_1_sum_of_quantity_times_unit_price(sqlite_app):
    """Canonical bar row 1: SUM(quantity * unit_price) — impossible before item
    100 because there was no `*` operator AND an aggregate argument could only
    be a bare column name."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "order_items.quantity"},
                        "right": {"col": "order_items.unit_price"},
                    },
                    "as": "revenue",
                }
            ],
        },
    )
    raw = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": ["order_items.quantity", "order_items.unit_price"],
            "limit": 100,
        },
    )
    expected = sum(float(r["quantity"]) * float(r["unit_price"]) for r in raw)
    assert len(raw) < 100, "corpus must fit under the limit or the ground truth is truncated"
    assert float(computed[0]["revenue"]) == pytest.approx(expected)


@pytest.mark.asyncio
async def test_bar_row_2_conditional_aggregation_per_group(sqlite_app):
    """Canonical bar row 2: per-group SUM(CASE WHEN status=... THEN amount ELSE
    0 END). The CASE sits INSIDE the aggregate — the position that was
    unreachable when CASE was only a top-level select item."""
    computed = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.customer_id",
                {
                    "fn": "sum",
                    "arg": {
                        "when": [
                            {
                                "when": {"col": "orders.status", "op": "eq", "value": "completed"},
                                "then": {"col": "orders.total_amount"},
                            }
                        ],
                        "else": {"literal": 0},
                    },
                    "as": "completed_total",
                },
            ],
            "group_by": ["orders.customer_id"],
            "limit": 100,
        },
    )
    raw = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": ["orders.customer_id", "orders.status", "orders.total_amount"],
            "limit": 100,
        },
    )
    expected: dict = {}
    for row in raw:
        amount = float(row["total_amount"]) if row["status"] == "completed" else 0.0
        expected[row["customer_id"]] = expected.get(row["customer_id"], 0.0) + amount
    got = {r["customer_id"]: float(r["completed_total"]) for r in computed}
    assert got == pytest.approx(expected)
    # The conditional must actually discriminate, or this passes vacuously.
    assert any(r["status"] != "completed" for r in raw)


@pytest.mark.asyncio
async def test_bar_row_7_top_category_per_group_by_computed_revenue(sqlite_app):
    """Canonical bar row 7: ranking groups by a COMPUTED measure. Only the
    arithmetic was missing — top_n already worked over a select alias."""
    ranked = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "order_items.quantity"},
                        "right": {"col": "order_items.unit_price"},
                    },
                    "as": "revenue",
                },
            ],
            "group_by": ["order_items.product_name"],
            "top_n": {"partition_by": [], "order_by": [{"col": "revenue", "dir": "desc"}], "n": 1},
            "limit": 100,
        },
    )
    everything = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                "order_items.product_name",
                {
                    "fn": "sum",
                    "arg": {
                        "op": "*",
                        "left": {"col": "order_items.quantity"},
                        "right": {"col": "order_items.unit_price"},
                    },
                    "as": "revenue",
                },
            ],
            "group_by": ["order_items.product_name"],
            "limit": 100,
        },
    )
    assert len(everything) > 1, "need multiple groups for a top-1 to mean anything"
    best = max(everything, key=lambda r: float(r["revenue"]))
    assert len(ranked) == 1
    assert ranked[0]["product_name"] == best["product_name"]


@pytest.mark.asyncio
async def test_guarded_division_returns_null_instead_of_failing(sqlite_app):
    """2026-07-25 Decision Log: `/` renders `left / NULLIF(right, 0)`. A zero
    denominator must yield a NULL cell, NOT a failed request — the behavior the
    entry chose over Postgres's error and MSSQL's whole-transaction abort."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": [
                {
                    "expr": {
                        "op": "/",
                        "left": {"col": "order_items.unit_price"},
                        # `quantity - quantity` is identically zero for every
                        # row, so this exercises the guard on real data without
                        # depending on the seed containing a zero.
                        "right": {
                            "op": "-",
                            "left": {"col": "order_items.quantity"},
                            "right": {"col": "order_items.quantity"},
                        },
                    },
                    "as": "ratio",
                }
            ],
            "limit": 5,
        },
    )
    assert rows, "expected rows, not an empty result"
    assert all(r["ratio"] is None for r in rows)


@pytest.mark.asyncio
async def test_nested_functions_and_cast_execute(sqlite_app):
    rows = await _rows(
        sqlite_app,
        {
            "from": "customers",
            "select": [
                "customers.name",
                {
                    "expr": {
                        "fn": "upper",
                        "args": [{"fn": "trim", "args": [{"col": "customers.name"}]}],
                    },
                    "as": "shout",
                },
                {
                    "expr": {"cast": {"col": "customers.id"}, "to": "text"},
                    "as": "id_text",
                },
            ],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        assert row["shout"] == row["name"].strip().upper()
        assert row["id_text"] == str(row["id_text"])


@pytest.mark.asyncio
async def test_group_by_a_computed_case_bucket(sqlite_app):
    """A computed GROUP BY key is expressed by projecting the expression and
    grouping by its alias — the route date_bucket has always used, so no second
    inline grammar for group keys was needed."""
    grouped = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                {
                    "expr": {
                        "when": [
                            {
                                "when": {"col": "orders.total_amount", "op": "gte", "value": 200},
                                "then": {"literal": "large"},
                            }
                        ],
                        "else": {"literal": "small"},
                    },
                    "as": "size_bucket",
                },
                {"fn": "count", "col": "*", "as": "n"},
            ],
            "group_by": ["size_bucket"],
            "limit": 100,
        },
    )
    raw = await _rows(
        sqlite_app, {"from": "orders", "select": ["orders.total_amount"], "limit": 100}
    )
    expected: dict = {}
    for row in raw:
        bucket = "large" if float(row["total_amount"]) >= 200 else "small"
        expected[bucket] = expected.get(bucket, 0) + 1
    assert {r["size_bucket"]: r["n"] for r in grouped} == expected
    assert len(expected) == 2, "corpus must land in both buckets or the grouping is untested"


@pytest.mark.asyncio
async def test_expression_predicate_filters_rows(sqlite_app):
    threshold = 100
    filtered = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": ["order_items.id"],
            "where": {
                "expr": {
                    "op": "*",
                    "left": {"col": "order_items.quantity"},
                    "right": {"col": "order_items.unit_price"},
                },
                "op": "gt",
                "value": threshold,
            },
            "limit": 100,
        },
    )
    raw = await _rows(
        sqlite_app,
        {
            "from": "order_items",
            "select": ["order_items.id", "order_items.quantity", "order_items.unit_price"],
            "limit": 100,
        },
    )
    expected = {r["id"] for r in raw if float(r["quantity"]) * float(r["unit_price"]) > threshold}
    assert {r["id"] for r in filtered} == expected
    assert 0 < len(expected) < len(raw), "the predicate must actually discriminate"
