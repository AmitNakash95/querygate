"""End-to-end execution of a searched HAVING and a searched CASE (TODO.md item 99).

Proves the two new WhereNode positions genuinely execute against a real (SQLite)
database with the correct boolean semantics — an OR over aggregate conditions in
HAVING really OR-combines, and a multi-condition CASE `when` really evaluates the
whole boolean tree — by comparing against a ground truth computed from the same
pipeline (so any guardrail applies equally on both sides).
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


@pytest.mark.asyncio
async def test_searched_having_or_over_aggregates_executes(sqlite_app):
    grouped = {
        "from": "orders",
        "select": [
            "orders.status",
            {"fn": "sum", "col": "orders.total_amount", "as": "s"},
            {"fn": "count", "col": "*", "as": "n"},
        ],
        "group_by": ["orders.status"],
        "limit": 100,
    }
    searched_having = {
        **grouped,
        "having": {
            "or": [
                {"col": "s", "op": "gt", "value": 500},
                {"col": "n", "op": "lt", "value": 2},
            ]
        },
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        base_resp = await client.post("/api/v1/demo/query", json=grouped)
        having_resp = await client.post("/api/v1/demo/query", json=searched_having)

    assert base_resp.status_code == 200, base_resp.text
    assert having_resp.status_code == 200, having_resp.text

    # Ground truth: filter the base groups by the SAME OR predicate in Python.
    expected = {
        row["status"] for row in base_resp.json()["rows"] if float(row["s"]) > 500 or row["n"] < 2
    }
    got = {row["status"] for row in having_resp.json()["rows"]}
    assert got == expected
    # The corpus must actually exercise OR (some group passes only one arm), or
    # this test proves nothing about the boolean combination.
    only_sum = {row["status"] for row in base_resp.json()["rows"] if float(row["s"]) > 500}
    only_count = {row["status"] for row in base_resp.json()["rows"] if row["n"] < 2}
    assert only_sum != only_count and expected == (only_sum | only_count)


@pytest.mark.asyncio
async def test_searched_case_and_condition_executes(sqlite_app):
    rows_query = {
        "from": "orders",
        "select": ["orders.id", "orders.status", "orders.total_amount"],
        "limit": 100,
    }
    case_query = {
        "from": "orders",
        "select": [
            "orders.id",
            {
                "when": [
                    {
                        "when": {
                            "and": [
                                {"col": "orders.status", "op": "eq", "value": "completed"},
                                {"col": "orders.total_amount", "op": "gt", "value": 100},
                            ]
                        },
                        "then": {"literal": "big-done"},
                    }
                ],
                "else": {"literal": "other"},
                "as": "label",
            },
        ],
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        rows_resp = await client.post("/api/v1/demo/query", json=rows_query)
        case_resp = await client.post("/api/v1/demo/query", json=case_query)

    assert rows_resp.status_code == 200, rows_resp.text
    assert case_resp.status_code == 200, case_resp.text

    expected = {
        row["id"]: (
            "big-done"
            if row["status"] == "completed" and float(row["total_amount"]) > 100
            else "other"
        )
        for row in rows_resp.json()["rows"]
    }
    got = {row["id"]: row["label"] for row in case_resp.json()["rows"]}
    assert got == expected
    # Both branches must be represented, or the AND-condition isn't really tested.
    assert "big-done" in got.values() and "other" in got.values()
