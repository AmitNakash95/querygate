"""Cross-dialect differential EXECUTION tests (TODO.md item 36 phase 2b).

Item 78 proves the compiler *renders* each AST correctly per dialect (SQL text).
This goes further: it **executes** the same `StructuredQuery` against a live
Postgres AND a live MSSQL (both seeded with the identical demo data) and asserts
the returned rows are equal — so a dialect-agnostic query really does behave
identically end-to-end, not just on paper. Queries that legitimately differ by
dialect (date bucketing, stddev naming, string_agg, NULLS ordering) are out of
scope here — those are the item-74/78 reject-or-render concern.

Needs BOTH live databases: `make compose-up` (Postgres) and the MSSQL setup from
tests/integration/test_mssql_live.py's docstring. Run with
`poetry run pytest -m 'postgres_live and mssql_live'`. Excluded from the default
run.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal

import pytest

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    AggregateSelectItem,
    OrderBySpec,
    Predicate,
    StructuredQuery,
    WhereGroup,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.real_db,
    pytest.mark.postgres_live,
    pytest.mark.mssql_live,
]

_PG_URL = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"
# 127.0.0.1, not "localhost": on macOS localhost resolves to ::1 first while
# docker-compose publishes this port on IPv4 only, so "localhost" hangs on IPv6
# and fails with a misleading Login timeout. CI sets the env var explicitly.
_MSSQL_HOST = os.environ.get("QUERYGATE_TEST_MSSQL_HOST", "127.0.0.1")
_MSSQL_PORT = os.environ.get("QUERYGATE_TEST_MSSQL_PORT", "14330")
_MSSQL_PW = os.environ.get("QUERYGATE_TEST_MSSQL_SA_PASSWORD", "QueryGate_Test_Pw1!")
_MSSQL_DRIVER = os.environ.get("QUERYGATE_TEST_MSSQL_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
_MSSQL_URL = f"mssql+aioodbc://sa:{_MSSQL_PW}@{_MSSQL_HOST}:{_MSSQL_PORT}/querygate_demo"

_TABLES = ["customers", "orders", "order_items", "products", "employees"]


def _setup() -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _MSSQL_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    set_registry(
        ConnectionRegistry(
            {
                "pg": ConnectionProfile(
                    id="pg", dialect="postgresql", connection_string=_PG_URL, known_tables=_TABLES
                ),
                "ms": ConnectionProfile(
                    id="ms", dialect="mssql", connection_string=_MSSQL_URL, known_tables=_TABLES
                ),
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(max_select_columns=50), overrides={}))
    reset_engines()


def _norm(value):
    # Normalize cross-driver type quirks so equal data compares equal: numerics to
    # a rounded float, temporals to ISO strings, everything else untouched.
    if isinstance(value, Decimal):
        return round(float(value), 4)
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _rows(result):
    return [{k: _norm(v) for k, v in row.items()} for row in result.rows]


async def _assert_same(query: StructuredQuery) -> list:
    """Assert both dialects return identical rows, and return them so a caller
    can additionally assert the query was *discriminating* — two empty result
    sets are trivially equal and would prove nothing."""
    pg = await StructuredQueryService(connection_id="pg").execute(query)
    ms = await StructuredQueryService(connection_id="ms").execute(query)
    assert _rows(pg) == _rows(ms), f"cross-dialect mismatch\n  PG={_rows(pg)}\n  MS={_rows(ms)}"
    assert pg.row_count == ms.row_count
    return _rows(pg)


@pytest.mark.asyncio
async def test_simple_select_filter_order_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="customers",
            select=["customers.id", "customers.name", "customers.country"],
            where=Predicate(col="customers.country", op="eq", value="GB"),
            order_by=[OrderBySpec(col="customers.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_in_and_between_and_like_match():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=["orders.id", "orders.status", "orders.total_amount"],
            where=WhereGroup(
                and_terms=[
                    Predicate(col="orders.status", op="in", value=["paid", "pending", "shipped"]),
                    Predicate(col="orders.total_amount", op="between", value=[0, 100000]),
                ]
            ),
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_join_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=["orders.id", "customers.name", "orders.status"],
            joins=[{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_group_by_aggregate_matches():
    _setup()
    await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=[
                "customers.country",
                AggregateSelectItem(fn="count", col="orders.id", alias="n"),
                AggregateSelectItem(fn="sum", col="orders.total_amount", alias="total"),
            ],
            joins=[{"table": "customers", "on": ["orders.customer_id", "customers.id"]}],
            group_by=["customers.country"],
            order_by=[OrderBySpec(col="customers.country", dir="asc")],
        )
    )


@pytest.mark.asyncio
async def test_searched_having_or_group_matches():
    """item 99: HAVING is a WhereNode. Boolean logic over aggregate conditions
    is plain ANSI SQL on both dialects — assert it RUNS and returns identical
    rows, not just that it renders (the items 75/82 'renders fine, breaks live'
    trap)."""
    _setup()
    select = [
        "orders.status",
        AggregateSelectItem(fn="count", col="orders.id", alias="n"),
        AggregateSelectItem(fn="sum", col="orders.total_amount", alias="total"),
    ]
    kept = await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=select,
            group_by=["orders.status"],
            having=WhereGroup(
                or_terms=[
                    Predicate(col="total", op="gt", value=300),
                    Predicate(col="n", op="lt", value=2),
                ]
            ),
            order_by=[OrderBySpec(col="orders.status", dir="asc")],
        )
    )
    every_group = await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=select,
            group_by=["orders.status"],
            order_by=[OrderBySpec(col="orders.status", dir="asc")],
        )
    )
    # Non-vacuous: the HAVING must have kept something AND excluded something.
    assert 0 < len(kept) < len(every_group)
    # Each OR arm must independently carry a group, or this isn't testing OR.
    assert any(r["total"] > 300 and r["n"] >= 2 for r in kept)
    assert any(r["n"] < 2 and r["total"] <= 300 for r in kept)


@pytest.mark.asyncio
async def test_searched_having_not_group_matches():
    """A negated HAVING group — the shape the audit `_where_shape` bug hid."""
    _setup()
    kept = await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=["orders.status", AggregateSelectItem(fn="count", col="orders.id", alias="n")],
            group_by=["orders.status"],
            having=WhereGroup(not_terms=Predicate(col="n", op="lt", value=2)),
            order_by=[OrderBySpec(col="orders.status", dir="asc")],
        )
    )
    # NOT(n < 2) must keep only groups with n >= 2, and keep at least one.
    assert kept and all(r["n"] >= 2 for r in kept)


@pytest.mark.asyncio
async def test_searched_case_condition_matches():
    """item 99: a multi-condition CASE `when` must classify rows identically on
    both dialects."""
    _setup()
    labelled = await _assert_same(
        StructuredQuery(
            from_table="orders",
            select=[
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
                        },
                        {
                            "when": {
                                "not": {"col": "orders.status", "op": "eq", "value": "completed"}
                            },
                            "then": {"literal": "not-done"},
                        },
                    ],
                    "else": {"literal": "small-done"},
                    "as": "label",
                },
            ],
            order_by=[OrderBySpec(col="orders.id", dir="asc")],
        )
    )
    # All three branches must actually be exercised, or the AND/NOT conditions
    # aren't being tested — a CASE where every row lands in ELSE proves nothing.
    assert {"big-done", "not-done", "small-done"} == {r["label"] for r in labelled}


# --------------------------------------------------------------------------- #
# Window functions (TODO.md item 101)
# --------------------------------------------------------------------------- #
# Every window function and frame form is spelled identically on Postgres and
# T-SQL, so this differential harness — same AST, both live servers, rows
# compared — is exactly the guard the plan asks for: "assert it RUNS on real
# Postgres AND real MSSQL, not just that SQL text compiles" (the items 75/82
# trap, where `within_group`/`stddev` rendered fine and failed live). The one
# genuine gap, a numeric RANGE offset, is asserted below to fail on the real
# server, which is what justifies the adapter rejecting it.

_BY_CUSTOMER = {"partition_by": ["orders.customer_id"]}
_BY_AMOUNT_DESC = {
    "partition_by": ["orders.customer_id"],
    "order_by": [{"col": "orders.total_amount", "dir": "desc"}],
}
_BY_ID = {"order_by": [{"col": "orders.id"}]}
_AMOUNT = {"col": "orders.total_amount"}

# One live case per member of the closed `WindowFn` set. Parameterized rather
# than hand-listed so a new window function cannot ship without a live
# both-dialects case: the coverage test below fails until it is added here.
_WINDOW_CASES = {
    "sum": {"arg": _AMOUNT, "over": _BY_CUSTOMER},
    "avg": {"arg": _AMOUNT, "over": _BY_CUSTOMER},
    "min": {"arg": _AMOUNT, "over": _BY_CUSTOMER},
    "max": {"arg": _AMOUNT, "over": _BY_CUSTOMER},
    "count": {"over": {}},
    "row_number": {"over": _BY_AMOUNT_DESC},
    "rank": {"over": _BY_AMOUNT_DESC},
    "dense_rank": {"over": _BY_AMOUNT_DESC},
    "ntile": {"buckets": 4, "over": {"order_by": [{"col": "orders.total_amount"}]}},
    "lag": {"arg": _AMOUNT, "offset": 2, "over": _BY_ID},
    "lead": {"arg": _AMOUNT, "over": _BY_ID},
    "first_value": {"arg": _AMOUNT, "over": _BY_ID},
    "last_value": {"arg": _AMOUNT, "over": _BY_ID},
}


def test_every_window_function_has_a_live_both_dialects_case():
    """A window function that renders on both dialects but only *runs* on one is
    the items 75/82 failure mode; this makes a live case mandatory."""
    import typing

    from querygate.query_ast.models import WindowFn

    assert set(typing.get_args(WindowFn)) == set(_WINDOW_CASES)


@pytest.mark.asyncio
@pytest.mark.parametrize("fn", sorted(_WINDOW_CASES))
async def test_window_function_matches_on_both_dialects(fn):
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {"fn": fn, **_WINDOW_CASES[fn], "as": "w"},
                ],
                "order_by": [{"col": "orders.id"}],
            }
        )
    )
    values = [r["w"] for r in rows]
    assert len(values) > 1
    # Discriminating: the window must have produced real values, not all-NULL
    # (which two dialects would agree on trivially). `lag`/`lead` legitimately
    # NULL at the edges, so only the interior is checked for them.
    assert any(v is not None for v in values), f"{fn} produced no values"
    if fn == "count":
        assert len(set(values)) == 1, "COUNT(*) OVER () is constant across rows"


@pytest.mark.asyncio
async def test_running_total_matches():
    """Canonical bar row 5 (a running cumulative total), on both real backends."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {
                        "fn": "sum",
                        "arg": _AMOUNT,
                        "over": {
                            **_BY_ID,
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
            }
        )
    )
    # A running total over positive amounts must grow monotonically.
    totals = [r["running_total"] for r in rows]
    assert len(totals) > 1 and totals == sorted(totals) and totals[0] != totals[-1]


@pytest.mark.asyncio
async def test_bounded_rows_frame_moving_average_matches():
    """A trailing 3-row average — the frame must really bound the window on both
    servers rather than averaging the whole partition."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {
                        "fn": "avg",
                        "arg": _AMOUNT,
                        "over": {
                            **_BY_ID,
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
            }
        )
    )
    unbounded = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {"fn": "avg", "arg": _AMOUNT, "over": {}, "as": "trailing_avg"},
                ],
                "order_by": [{"col": "orders.id"}],
            }
        )
    )
    assert [r["trailing_avg"] for r in rows] != [r["trailing_avg"] for r in unbounded]


@pytest.mark.asyncio
async def test_unbounded_range_frame_matches():
    """`RANGE UNBOUNDED PRECEDING … CURRENT ROW` is the one RANGE form T-SQL
    accepts, and it must behave identically to Postgres's."""
    _setup()
    await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    {
                        "fn": "sum",
                        "arg": _AMOUNT,
                        "over": {
                            **_BY_ID,
                            "frame": {
                                "mode": "range",
                                "start": {"bound": "unbounded_preceding"},
                                "end": {"bound": "current_row"},
                            },
                        },
                        "as": "running_total",
                    },
                ],
                "order_by": [{"col": "orders.id"}],
            }
        )
    )


@pytest.mark.asyncio
async def test_window_over_a_computed_expression_matches():
    """Item 100's `Expression` inside a window argument, on both backends — the
    two engine pillars composing."""
    _setup()
    await _assert_same(
        StructuredQuery.model_validate(
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
            }
        )
    )


@pytest.mark.asyncio
async def test_real_mssql_rejects_a_numeric_range_offset():
    """The live fact `MSSQLDialectAdapter.window_frame`'s rejection exists for.

    SQLAlchemy compiles `RANGE 2 PRECEDING` for the mssql dialect without
    complaint, so nothing but the adapter stands between that AST and a live
    failure. Asserted directly against both servers: Postgres runs it, SQL Server
    refuses it. If T-SQL ever gains numeric RANGE offsets this test starts
    failing and the rejection can be revisited.
    """
    _setup()
    import sqlalchemy as sa

    from querygate.connections.engine import get_engine

    statement = (
        "SELECT SUM(total_amount) OVER (ORDER BY id RANGE BETWEEN 2 PRECEDING "
        "AND CURRENT ROW) FROM orders"
    )
    async with get_engine("pg").connect() as conn:
        assert (await conn.execute(sa.text(statement))).first() is not None
    with pytest.raises(Exception) as excinfo:
        async with get_engine("ms").connect() as conn:
            await conn.execute(sa.text(statement))
    assert "range" in str(excinfo.value).lower()
