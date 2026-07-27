"""End-to-end execution of set operations — UNION / INTERSECT / EXCEPT (item 104).

Proves the compound statement actually runs against a real (SQLite) database and
returns the set-theoretic answer, rather than merely rendering plausible SQL —
the items 75/82 distinction this repo treats as the difference between a claim
and a fact. The set-theoretic identities asserted here (`A ∪ B` equals the union
of the two arms run separately, `A ∩ B` its intersection, `A − B` its difference)
are what would break if an arm were silently dropped, filtered by the wrong
scope's WHERE, or de-duplicated when the caller asked for duplicates.

Row 8 of the regression bar in `docs/ENGINE_EXPRESSIVENESS_PLAN.md` §5 — a UNION
of a high-value and a dormant customer segment — is covered by
`test_regression_bar_row_8_union_of_two_customer_segments`.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


def _arm(where: dict) -> dict:
    # `orders.id`, not `orders.customer_id`, and that choice is what makes the
    # identity tests below discriminating. With `customer_id` the demo seed makes
    # the "big" segment a strict SUBSET of the "completed" segment, so
    # `paid | big == paid` and the UNION assertion held even if arm 2 were dropped
    # entirely — a test that stated it could not be faked, and could.
    return {"from": "orders", "select": ["orders.id"], "where": where}


_DONE = {"col": "orders.status", "op": "eq", "value": "completed"}
_BIG = {"col": "orders.total_amount", "op": "gt", "value": 100}


async def _ids(client: AsyncClient, query: dict, key: str = "id") -> list:
    response = await client.post("/api/v1/demo/query", json=query)
    assert response.status_code == 200, response.text
    return [row[key] for row in response.json()["rows"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op,expected",
    [
        ("union", lambda paid, big: set(paid) | set(big)),
        ("intersect", lambda paid, big: set(paid) & set(big)),
        ("except", lambda paid, big: set(paid) - set(big)),
    ],
)
async def test_set_operation_matches_the_same_arms_combined_client_side(sqlite_app, op, expected):
    """Each operator returns exactly what combining the two arms' own result sets
    client-side would produce. This is the check that a dropped or mis-scoped arm
    cannot pass: a `UNION` that silently ignored arm 2 still returns rows."""
    combined = {**_arm(_DONE), "set_op": {"op": op, "arms": [_arm(_BIG)]}, "limit": 100}
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        paid = await _ids(client, {**_arm(_DONE), "limit": 100})
        big = await _ids(client, {**_arm(_BIG), "limit": 100})
        actual = await _ids(client, combined)

    assert set(actual) == expected(paid, big)
    # Guard against a vacuous pass. All THREE differences must be non-empty, one
    # per operator: `paid - big` makes EXCEPT meaningful, `paid & big` makes
    # INTERSECT meaningful, and `big - paid` is the one UNION needs — without it
    # `paid | big == paid` and an implementation ignoring arm 2 still passes.
    assert set(paid) - set(big), "EXCEPT would be vacuous"
    assert set(paid) & set(big), "INTERSECT would be vacuous"
    assert set(big) - set(paid), "UNION would be vacuous - arm 2 adds nothing"


@pytest.mark.asyncio
async def test_union_de_duplicates_and_union_all_keeps_duplicates(sqlite_app):
    """`all` is the only flag that changes the row multiset, so it gets its own
    execution check — a compiler that dropped the flag would still return correct
    *distinct* rows and pass every assertion above."""
    base = _arm(_DONE)
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        one_arm = await _ids(client, {**base, "limit": 100})
        distinct = await _ids(
            client, {**base, "set_op": {"op": "union", "arms": [base]}, "limit": 100}
        )
        with_all = await _ids(
            client,
            {**base, "set_op": {"op": "union", "all": True, "arms": [base]}, "limit": 100},
        )

    # A query unioned with itself: UNION collapses to the arm's DISTINCT values,
    # UNION ALL returns the arm's rows twice over.
    assert one_arm
    assert sorted(distinct) == sorted(set(one_arm))
    assert len(with_all) == 2 * len(one_arm)


@pytest.mark.asyncio
async def test_regression_bar_row_8_union_of_two_customer_segments(sqlite_app):
    """The canonical analyst query from the plan's §5 table, row 8: one result set
    combining a high-value segment and a dormant segment, each with its own
    filter and a literal label, ordered and limited as one statement."""
    query = {
        "from": "customers",
        "select": [
            "customers.name",
            {"expr": {"literal": "high_value"}, "as": "segment"},
        ],
        "joins": [{"table": "orders", "on": ["customers.id", "orders.customer_id"]}],
        "where": {"col": "orders.total_amount", "op": "gt", "value": 300},
        "set_op": {
            "op": "union",
            "arms": [
                {
                    "from": "customers",
                    "select": [
                        "customers.name",
                        {"expr": {"literal": "dormant"}, "as": "segment"},
                    ],
                    "where": {"col": "customers.country", "op": "eq", "value": "FR"},
                }
            ],
        },
        "order_by": [{"col": "name"}],
        "limit": 50,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query", json=query)

    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    segments = {row["segment"] for row in rows}
    assert segments == {"high_value", "dormant"}, rows
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)


@pytest.mark.asyncio
async def test_order_by_and_limit_apply_to_the_combined_result_not_one_arm(sqlite_app):
    """The clause placement the AST commits to: `order_by`/`limit` on the query
    that carries `set_op` bound the WHOLE statement. If they were applied to arm 1
    only, the response would carry arm-2 rows past the limit and out of order."""
    query = {
        "from": "orders",
        "select": ["orders.id"],
        "where": {"col": "orders.status", "op": "eq", "value": "completed"},
        "set_op": {
            "op": "union",
            "arms": [
                {
                    "from": "orders",
                    "select": ["orders.id"],
                    "where": {"col": "orders.status", "op": "neq", "value": "completed"},
                }
            ],
        },
        "order_by": [{"col": "id", "dir": "desc"}],
        "limit": 3,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        combined = await _ids(client, query)
        every = await _ids(client, {"from": "orders", "select": ["orders.id"], "limit": 100})

    assert len(combined) == 3
    assert combined == sorted(every, reverse=True)[:3]


@pytest.mark.asyncio
async def test_each_arm_gets_its_own_group_by_and_aggregate(sqlite_app):
    """An arm is a full query, not a projection list: it carries its own joins,
    grouping and aggregates. Proven by aggregating in both arms over different
    tables and reading both back from one response."""
    query = {
        "from": "orders",
        "select": [
            "orders.status",
            {"fn": "count", "col": "*", "as": "n"},
        ],
        "group_by": ["orders.status"],
        "set_op": {
            "op": "union",
            "arms": [
                {
                    "from": "customers",
                    "select": [
                        "customers.country",
                        {"fn": "count", "col": "*", "as": "n"},
                    ],
                    "group_by": ["customers.country"],
                }
            ],
        },
        "limit": 100,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query", json=query)

    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    labels = {row["status"] for row in rows}
    assert "completed" in labels  # an order status, from arm 1
    assert "US" in labels  # a country code, from arm 2
    assert all(row["n"] >= 1 for row in rows)


@pytest.mark.asyncio
async def test_intersect_all_is_rejected_before_it_reaches_the_database(sqlite_app):
    """SQLite (like MSSQL) has no `INTERSECT ALL`; SQLAlchemy renders it happily
    and the server then fails with a syntax error. The adapter rejects it as a
    typed 4xx instead — reject-don't-emulate, and a clean error rather than a 500."""
    query = {
        "from": "orders",
        "select": ["orders.id"],
        "set_op": {
            "op": "intersect",
            "all": True,
            "arms": [{"from": "orders", "select": ["orders.id"]}],
        },
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query", json=query)

    assert response.status_code == 422, response.text
    assert "INTERSECT ALL" in response.text


@pytest.mark.asyncio
async def test_explain_reports_every_arms_tables_not_just_the_first(sqlite_app):
    """`ExplainResult.tables` and `ExplainResult.sql` describe the same statement,
    so they must not contradict each other. Before this, a three-arm union named
    all three tables in `sql` and one of them in `tables` (TODO.md item 121)."""
    query = {
        "from": "orders",
        "select": ["orders.id"],
        "set_op": {
            "op": "union",
            "arms": [
                {"from": "customers", "select": ["customers.id"]},
                {"from": "products", "select": ["products.id"]},
            ],
        },
        "limit": 10,
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query/explain", json=query)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body["tables"]) == {"orders", "customers", "products"}, body["tables"]
    for table in ("orders", "customers", "products"):
        assert table in body["sql"]


@pytest.mark.asyncio
async def test_explain_reports_a_nested_subquerys_table_too(sqlite_app):
    """The same fix covers the container item 97 added — an `IN (subquery)`'s table
    was equally missing from `tables` while appearing in `sql`."""
    query = {
        "from": "orders",
        "select": ["orders.id"],
        "where": {
            "col": "orders.customer_id",
            "op": "in",
            "value_subquery": {"from": "customers", "select": ["customers.id"]},
        },
    }
    async with AsyncClient(transport=ASGITransport(app=sqlite_app), base_url=_BASE_URL) as client:
        response = await client.post("/api/v1/demo/query/explain", json=query)

    assert response.status_code == 200, response.text
    assert set(response.json()["tables"]) == {"orders", "customers"}
