"""End-to-end execution of item 103's join forms through the real pipeline.

`tests/unit/test_nonequi_joins.py` proves the SQL *text*; this proves the joins
return the right **rows** against a real (SQLite) database via the one request
pipeline — REST in, rows out, every guardrail applied.

Ground truth is derived from a second query through the SAME pipeline wherever
possible, so the comparison isolates the join form rather than trusting a
hand-written expectation. That matters most for FULL OUTER, whose whole content
is the rows an INNER join drops.

The last test is ENGINE_EXPRESSIVENESS_PLAN.md §5 regression-bar row 16 — a
price-band join — which is the wall this item exists to remove. The PG-vs-MSSQL
value comparison for the same shapes is in
`tests/integration/test_cross_dialect_differential.py`.
"""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

# SQLite only learned FULL OUTER JOIN in 3.39 (2022-06). SQLite is not a supported
# registry dialect — it stands in for a real server here — so an older bundled
# library is an environment limitation, not a product defect. Skip loudly rather
# than failing with an opaque OperationalError that reads like a compiler bug.
_needs_full_outer = pytest.mark.skipif(
    sqlite3.sqlite_version_info < (3, 39),
    reason=f"SQLite {sqlite3.sqlite_version} has no FULL OUTER JOIN (needs >= 3.39)",
)

_BASE_URL = "http://localhost"


async def _post(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.post("/api/v1/demo/query", json=body)


async def _rows(app, body):
    resp = await _post(app, body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


# --- range / non-equi joins -------------------------------------------------


async def test_inequality_join_returns_only_the_pairs_that_satisfy_it(sqlite_app):
    """`orders JOIN customers ON orders.customer_id > customers.id` — a genuine
    non-equi join. Every returned pair must actually satisfy the inequality, and
    the result must be strictly larger than the equality join, or the ON clause
    could have been silently compiled as `=`."""
    pairs = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": ["orders.id", "orders.customer_id", "customers.id"],
            "joins": [
                {
                    "table": "customers",
                    "condition": {
                        "col": "orders.customer_id",
                        "op": "gt",
                        "value_col": "customers.id",
                    },
                }
            ],
            "order_by": [{"col": "orders.id", "dir": "asc"}],
            "limit": 100,
        },
    )
    assert pairs, "join returned nothing — the assertions below would be vacuous"
    for row in pairs:
        assert row["customer_id"] > row["id_1"], row

    equality = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": ["orders.id"],
            "joins": [{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
            "limit": 100,
        },
    )
    assert len(pairs) > len(equality)


async def test_a_degenerate_range_join_agrees_with_the_equality_sugar(sqlite_app):
    """`>= x AND <= x` is `= x`. Running both forms through the pipeline pins the
    range path to the equality path it must generalize — if `condition` compiled
    the wrong operands, or ANDed its terms as OR, these two would diverge."""
    select = ["orders.id", "products.id"]
    order = [{"col": "orders.id", "dir": "asc"}, {"col": "products.id", "dir": "asc"}]

    via_condition = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": select,
            "joins": [
                {
                    "table": "products",
                    "condition": {
                        "and": [
                            {
                                "col": "orders.total_amount",
                                "op": "gte",
                                "value_col": "products.price",
                            },
                            {
                                "col": "orders.total_amount",
                                "op": "lte",
                                "value_col": "products.price",
                            },
                        ]
                    },
                }
            ],
            "order_by": order,
            "limit": 100,
        },
    )
    via_on = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": select,
            "joins": [{"table": "products", "on": ["orders.total_amount", "products.price"]}],
            "order_by": order,
            "limit": 100,
        },
    )
    assert via_condition, "no order total equalled a product price — comparison is vacuous"
    assert via_condition == via_on


async def test_join_condition_may_reference_an_earlier_joined_table(sqlite_app):
    """Three-table condition: `JOIN order_items ON order_items.order_id =
    orders.id AND order_items.unit_price <= orders.total_amount`. The pair
    restriction `extra_on` carries is sugar-specific, not a property of a join."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "customers",
            "select": ["customers.id", "order_items.id"],
            "joins": [
                {"table": "orders", "on": ["customers.id", "orders.customer_id"]},
                {
                    "table": "order_items",
                    "condition": {
                        "and": [
                            {
                                "col": "order_items.order_id",
                                "op": "eq",
                                "value_col": "orders.id",
                            },
                            {
                                "col": "order_items.unit_price",
                                "op": "lte",
                                "value_col": "orders.total_amount",
                            },
                        ]
                    },
                },
            ],
            "limit": 100,
        },
    )
    assert rows


async def test_forward_reference_in_a_join_condition_is_rejected(sqlite_app):
    """`orders`' condition names `order_items`, which is joined after it."""
    resp = await _post(
        sqlite_app,
        {
            "from": "customers",
            "select": ["customers.id"],
            "joins": [
                {
                    "table": "orders",
                    "condition": {
                        "and": [
                            {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
                            {
                                "col": "orders.id",
                                "op": "eq",
                                "value_col": "order_items.order_id",
                            },
                        ]
                    },
                },
                {"table": "order_items", "on": ["order_items.order_id", "orders.id"]},
            ],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "before it is joined" in resp.text


# --- FULL OUTER -------------------------------------------------------------


# `customers.id = products.id` is a deliberately artificial key, chosen because
# the demo seed makes it the one join in this schema with genuinely unmatched
# rows on a known side: customers are ids 1-8 and products 1-10, so products 9
# and 10 match nothing. Every natural FK in the seed is fully satisfied, which
# would make an outer-join test pass while proving nothing.
_MISMATCHED_JOIN = ["customers.id", "products.id"]
_OUTER_SELECT = ["customers.id", "products.id"]


async def _join_rows(app, join_type):
    join = {"table": "products", "on": _MISMATCHED_JOIN}
    if join_type != "inner":
        join["type"] = join_type
    return await _rows(
        app,
        {
            "from": "customers",
            "select": _OUTER_SELECT,
            "joins": [join],
            "limit": 200,
        },
    )


@_needs_full_outer
async def test_full_outer_join_keeps_rows_that_an_inner_and_a_left_both_drop(sqlite_app):
    """The defining property: from `customers`, the unmatched rows live on the
    RIGHT, so an INNER and a LEFT join both drop them and only FULL keeps them —
    each surfacing with a NULL on the customers side."""
    inner = await _join_rows(sqlite_app, "inner")
    left = await _join_rows(sqlite_app, "left")
    full = await _join_rows(sqlite_app, "full")

    assert len(inner) == len(left), "no unmatched customer, so LEFT must equal INNER here"
    assert len(full) > len(inner), "FULL OUTER returned no extra rows — nothing is proven"

    extra = [row for row in full if row not in inner]
    assert extra
    for row in extra:
        assert row["id"] is None, row
        assert row["id_1"] is not None, row


@_needs_full_outer
async def test_left_join_is_not_silently_promoted_to_full(sqlite_app):
    """Regression guard for the compiler change: `isouter` and `full` became two
    flags on one `stmt.join(...)` call, so swapping or ORing them would turn every
    LEFT join into a FULL one.

    The DIRECTION matters, and an earlier version of this test got it backwards.
    From `products`, the unmatched rows (9 and 10) are on the side a LEFT join
    already keeps, so LEFT and FULL return identical rows and the test proved
    nothing. From `customers` they are on the far side: LEFT must DROP them and
    FULL must keep them, so the two counts differ and the mutation is caught.
    """
    left = await _join_rows(sqlite_app, "left")
    full = await _join_rows(sqlite_app, "full")

    assert len(full) > len(left), "LEFT and FULL agree here — the guard proves nothing"
    # A LEFT join FROM customers can never surface a NULL customer.
    assert all(row["id"] is not None for row in left)
    # ...and must not have kept the unmatched products that only FULL keeps.
    assert all(row["id_1"] is not None for row in left)


async def test_left_join_keeps_its_own_unmatched_rows(sqlite_app):
    """The other half of LEFT's contract, from the side where its unmatched rows
    live: a LEFT join FROM products drops no product and nulls out the far side."""
    products_left = await _rows(
        sqlite_app,
        {
            "from": "products",
            "select": ["products.id", "customers.id"],
            "joins": [{"table": "customers", "type": "left", "on": _MISMATCHED_JOIN}],
            "limit": 200,
        },
    )
    assert products_left
    assert all(row["id"] is not None for row in products_left)
    assert any(
        row["id_1"] is None for row in products_left
    ), "no unmatched product — test is vacuous"


# --- CROSS ------------------------------------------------------------------


async def test_cross_join_is_refused_by_default_policy(sqlite_app):
    resp = await _post(
        sqlite_app,
        {
            "from": "customers",
            "select": ["customers.id", "products.id"],
            "joins": [{"table": "products", "type": "cross"}],
        },
    )
    assert resp.status_code == 422, resp.text
    assert "allow_cross_join" in resp.text


async def test_cross_join_returns_the_cartesian_product_when_enabled(sqlite_app, monkeypatch):
    """Enabled, it must be a real product: |customers| x |products|."""
    from querygate.policy.loader import PolicyStore, set_policy_store
    from querygate.policy.models import Policy

    set_policy_store(PolicyStore(default=Policy(allow_cross_join=True), overrides={}))

    customers = await _rows(
        sqlite_app, {"from": "customers", "select": ["customers.id"], "limit": 500}
    )
    products = await _rows(
        sqlite_app, {"from": "products", "select": ["products.id"], "limit": 500}
    )
    crossed = await _rows(
        sqlite_app,
        {
            "from": "customers",
            "select": ["customers.id", "products.id"],
            "joins": [{"table": "products", "type": "cross"}],
            "limit": 500,
        },
    )
    assert len(crossed) == len(customers) * len(products)


# --- the regression bar -----------------------------------------------------


async def test_bar_row_16_price_band_join(sqlite_app):
    """ENGINE_EXPRESSIVENESS_PLAN.md §5 row 16: `ON price BETWEEN lo AND hi`.

    Expressed as a self-join of products against price bands built from the same
    table, since the demo schema has no band table — the shape under test is the
    range condition, not the band source. Each product must land in a band whose
    bounds really do contain its price.
    """
    rows = await _rows(
        sqlite_app,
        {
            "from": "products",
            "from_alias": "p",
            "select": ["p.name", "p.price", "band.price"],
            "joins": [
                {
                    "table": "products",
                    "alias": "band",
                    "condition": {
                        "and": [
                            {"col": "p.price", "op": "gte", "value_col": "band.price"},
                            {
                                "col": "p.price",
                                "op": "lte",
                                "value_expr": {
                                    "left": {"col": "band.price"},
                                    "op": "*",
                                    "right": {"literal": 2},
                                },
                            },
                        ]
                    },
                }
            ],
            "order_by": [{"col": "p.price", "dir": "asc"}],
            "limit": 200,
        },
    )
    assert rows, "no product fell in any band — the assertion below would be vacuous"
    for row in rows:
        # NUMERIC comes back as a decimal string over JSON; compare as numbers.
        price, band = float(row["price"]), float(row["price_1"])
        assert price >= band
        assert price <= band * 2
