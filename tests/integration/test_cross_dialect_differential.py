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
import pytest_asyncio

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import MandatoryRowFilter, Policy
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
    _setup_with_tables(_TABLES)


def _setup_with_tables(tables) -> None:
    from querygate.core.config import config as shared_config

    shared_config.odbc_driver = _MSSQL_DRIVER.replace(" ", "+")
    shared_config.db_trust_server_certificate = True
    set_registry(
        ConnectionRegistry(
            {
                "pg": ConnectionProfile(
                    id="pg", dialect="postgresql", connection_string=_PG_URL, known_tables=tables
                ),
                "ms": ConnectionProfile(
                    id="ms", dialect="mssql", connection_string=_MSSQL_URL, known_tables=tables
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
async def test_regression_bar_row_15_window_as_an_expression_operand_matches():
    """Row 15 of the regression bar (item 125) on both live backends: a window
    used as an OPERAND of arithmetic, not as the projection itself.

    This is the leg that a rendering assertion cannot supply. Both dialects spell
    `SUM(...) OVER (...)` identically, but the operand position puts the window
    inside item 100's GUARDED division — which compiles to a `NULLIF` plus a
    `CAST(... AS NUMERIC)` — and that composition is exactly the kind of thing
    that renders plausibly on both and evaluates differently (T-SQL's integer
    division and NUMERIC scale rules are not Postgres's). Asserting equal rows is
    the only way to know.
    """
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "order_items",
                "select": [
                    "order_items.id",
                    {
                        "expr": {
                            "op": "/",
                            "left": {"col": "order_items.quantity"},
                            "right": {
                                "fn": "sum",
                                "over": {"partition_by": ["order_items.order_id"]},
                                "arg": {"col": "order_items.quantity"},
                            },
                        },
                        "as": "share_of_order_qty",
                    },
                ],
                "order_by": [{"col": "order_items.id"}],
            }
        )
    )
    # Two empty result sets would be trivially equal, and an all-NULL column would
    # be equal too while proving the window computed nothing.
    assert rows, "no rows — the differential comparison would be vacuous"
    assert any(row["share_of_order_qty"] is not None for row in rows)


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


# --------------------------------------------------------------------------- #
# Item 102 — date/time primitives.
#
# This is the tier that matters most for this item, because two of its parts
# return a genuinely DIFFERENT NUMBER on each dialect natively:
#   * `dayofweek` — T-SQL's DATEPART(weekday) is 1-based and moves with the
#     server's SET DATEFIRST, where Postgres's `dow` is 0=Sunday..6=Saturday;
#   * `week` — T-SQL's plain `week` is a DATEFIRST-dependent count, not the ISO
#     week Postgres's `week` returns.
# Both adapters normalize to one documented definition. A rendering assertion
# cannot tell a correct normalization from a plausible-looking one — only
# running both servers and comparing the values can, which is the items 75/82
# lesson this suite exists for.
# --------------------------------------------------------------------------- #
_DATE_COLUMN = {"col": "orders.created_at"}

# Every unit that gets a live both-dialects case. Pinned against `IntervalUnit`
# by the coverage test below, so a new unit cannot ship without one.
_LIVE_INTERVAL_UNITS = ("year", "month", "week", "day", "hour", "minute", "second")

# Each part maps to a callable computing the expected value from a Python
# datetime — ground truth derived independently of BOTH dialects, so a shared
# mistake in the two adapters cannot make a wrong answer look right.
_DATE_PART_EXPECTATIONS = {
    "year": lambda d: d.year,
    "quarter": lambda d: (d.month - 1) // 3 + 1,
    "month": lambda d: d.month,
    "week": lambda d: d.isocalendar()[1],
    "day": lambda d: d.day,
    "dayofweek": lambda d: (d.weekday() + 1) % 7,
    "dayofyear": lambda d: d.timetuple().tm_yday,
    "hour": lambda d: d.hour,
    "minute": lambda d: d.minute,
    "second": lambda d: d.second,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("part", sorted(_DATE_PART_EXPECTATIONS))
async def test_extract_part_matches_on_both_dialects(part):
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    "orders.created_at",
                    {"expr": {"extract": _DATE_COLUMN, "part": part}, "as": "v"},
                ],
                "order_by": [{"col": "orders.id"}],
            }
        )
    )
    assert len(rows) > 1
    expected = _DATE_PART_EXPECTATIONS[part]
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["created_at"])
        assert row["v"] == expected(stamp), f"{part}: got {row['v']} for {stamp}"
        # Both dialects must agree on the TYPE too — Postgres's EXTRACT returns
        # numeric without the adapter's cast, which `_norm` would round into
        # equality with MSSQL's int and hide.
        assert isinstance(row["v"], int), f"{part} must be an integer on both dialects"


def test_every_interval_unit_has_a_live_both_dialects_case():
    """The `IntervalUnit` sibling of the DatePart coverage gate — previously the
    unit list below was hand-written, so a new unit would have shipped with no
    live case at all."""
    import typing

    from querygate.query_ast.models import IntervalUnit

    assert set(typing.get_args(IntervalUnit)) == set(_LIVE_INTERVAL_UNITS)


@pytest.mark.asyncio
@pytest.mark.parametrize("unit", sorted(_LIVE_INTERVAL_UNITS))
async def test_date_add_matches_on_both_dialects(unit):
    """Postgres shifts via `make_interval`, MSSQL via `DATEADD` — two entirely
    different mechanisms that must land on the same instant."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.id",
                    "orders.created_at",
                    {
                        "expr": {"date_add": _DATE_COLUMN, "unit": unit, "amount": -3},
                        "as": "shifted",
                    },
                ],
                "order_by": [{"col": "orders.id"}],
            }
        )
    )
    assert len(rows) > 1
    assert all(row["shifted"] is not None for row in rows), f"{unit} shift produced NULLs"
    assert any(row["shifted"] != row["created_at"] for row in rows), f"{unit} shift changed nothing"


@pytest.mark.asyncio
async def test_relative_date_filter_matches_on_both_dialects():
    """Canonical bar row 12 on both real backends: a lookback window with no
    caller-computed timestamp literal. Both clocks are UTC by construction
    (Postgres's session pin, MSSQL's SYSUTCDATETIME), so the two servers must
    select the identical rows even if their host clocks are configured for
    different zones."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id"],
                "where": {
                    "col": "orders.created_at",
                    "op": "gte",
                    "value_expr": {
                        "date_add": {"now": "timestamp"},
                        "unit": "day",
                        "amount": -3650,
                    },
                },
                "order_by": [{"col": "orders.id"}],
            }
        )
    )
    assert rows, "the relative window matched nothing — the assertion is vacuous"


@pytest.mark.asyncio
async def test_now_reads_the_same_utc_instant_on_both_dialects():
    """The timezone decision's live proof: `now` must be UTC on BOTH servers,
    not each server's local wall clock. Compared against the test process's own
    UTC clock rather than against each other, so two identically-misconfigured
    servers cannot agree on a wrong answer."""
    _setup()
    query = StructuredQuery.model_validate(
        {"from": "orders", "select": [{"expr": {"now": "timestamp"}, "as": "t"}], "limit": 1}
    )
    for connection in ("pg", "ms"):
        result = await StructuredQueryService(connection_id=connection).execute(query)
        reading = result.rows[0]["t"]
        if isinstance(reading, str):
            reading = dt.datetime.fromisoformat(reading)
        reading = reading.replace(tzinfo=None)
        drift = abs(
            (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - reading).total_seconds()
        )
        assert drift < 300, f"{connection} clock is {drift}s from UTC — not a UTC reading"


@pytest.mark.asyncio
async def test_now_date_matches_on_both_dialects():
    """`now: "date"` executed on both real servers. It was previously proven
    only on SQLite and by inspecting the SQL text of an UNCONNECTED mssql
    dialect — the tier this repo trusts least, and the one that produced item
    100's wrong `CAST(x AS text)` rationale."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {"from": "orders", "select": [{"expr": {"now": "date"}, "as": "d"}], "limit": 1}
        )
    )
    today = dt.datetime.now(dt.timezone.utc).date()
    assert rows[0]["d"] in {today.isoformat(), (today - dt.timedelta(days=1)).isoformat()}


# --------------------------------------------------------------------------- #
# A purpose-built probe table on BOTH servers, for the date facts the shared
# demo corpus structurally cannot express.
#
# This exists because live mutation testing found the item-102 differential
# cases above could NOT catch two real defects:
#   * every seeded `created_at` is exactly midnight with no fractional part, so
#     the MSSQL side of the `EXTRACT(second …)` rounding (59.7 -> 60) had no
#     differential-tier case at all.
# It is NOT true that the corpus could not discriminate ISO week — 3 of the 20
# seeded dates diverge at DATEFIRST=7 (2025-01-05 is ISO 1 / T-SQL 2, plus
# 2024-11-10 and 2025-06-01). A `week` mutation once appeared to survive here, but
# that was a dead lookup map, not a thin corpus; blaming the corpus was a
# misdiagnosis, corrected in docs/TODO_ARCHIVE.md. The probe is kept anyway
# because 2024-12-30 (T-SQL week 53 vs ISO 1) is a far stronger discriminator
# than the incidental rows, and the fractional-second rows close a real gap.
# --------------------------------------------------------------------------- #
_PROBE_ROWS = [
    # (id, timestamp) — chosen so each row discriminates something specific.
    #
    # 2024-12-30 is the load-bearing one: it is ISO week 1 **of 2025**, while
    # T-SQL's DATEPART(week, …) counts it as week 53 of 2024. Any date away from
    # a year boundary has the two agreeing, which is why the demo corpus missed
    # it entirely.
    (1, "2024-12-30 10:20:30.600"),
    (2, "2025-01-01 13:45:59.700"),  # fractional second that ROUNDS UP to 60
    (3, "2025-06-15 23:59:59.900"),  # rounds up across an hour AND a day
]


async def _create_probe_tables() -> None:
    """Create + seed `date_probe` on both live servers, using each dialect's own
    DDL. Dropped first so a previous failed run cannot poison the data."""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    pg = create_async_engine(_PG_URL)
    async with pg.begin() as conn:
        await conn.execute(sa.text("DROP TABLE IF EXISTS date_probe"))
        await conn.execute(sa.text("CREATE TABLE date_probe (id int, at timestamp)"))
        for probe_id, stamp in _PROBE_ROWS:
            await conn.execute(
                sa.text(f"INSERT INTO date_probe VALUES ({probe_id}, TIMESTAMP '{stamp}')")
            )
    await pg.dispose()

    ms = create_async_engine(
        _MSSQL_URL + f"?driver={_MSSQL_DRIVER.replace(' ', '+')}&TrustServerCertificate=Yes"
    )
    async with ms.begin() as conn:
        await conn.execute(
            sa.text("IF OBJECT_ID('date_probe','U') IS NOT NULL DROP TABLE date_probe")
        )
        await conn.execute(sa.text("CREATE TABLE date_probe (id int, at datetime2(3))"))
        for probe_id, stamp in _PROBE_ROWS:
            await conn.execute(sa.text(f"INSERT INTO date_probe VALUES ({probe_id}, '{stamp}')"))
    await ms.dispose()


@pytest_asyncio.fixture
async def date_probe_tables():
    await _create_probe_tables()
    _setup_with_tables(_TABLES + ["date_probe"])
    try:
        yield
    finally:
        import sqlalchemy as sa
        from sqlalchemy.ext.asyncio import create_async_engine

        # Each server's drop gets its OWN try/finally: sharing one body means a
        # Postgres failure strands the MSSQL table until a later run's
        # drop-before-create happens to recover it.
        try:
            pg = create_async_engine(_PG_URL)
            async with pg.begin() as conn:
                await conn.execute(sa.text("DROP TABLE IF EXISTS date_probe"))
            await pg.dispose()
        finally:
            ms = create_async_engine(
                _MSSQL_URL + f"?driver={_MSSQL_DRIVER.replace(' ', '+')}&TrustServerCertificate=Yes"
            )
            async with ms.begin() as conn:
                await conn.execute(
                    sa.text("IF OBJECT_ID('date_probe','U') IS NOT NULL DROP TABLE date_probe")
                )
            await ms.dispose()
            reset_engines()


@pytest.mark.asyncio
@pytest.mark.parametrize("part", sorted(_DATE_PART_EXPECTATIONS))
async def test_extract_part_matches_on_both_dialects_at_the_hard_dates(part, date_probe_tables):
    """The same per-part comparison as above, over timestamps chosen to break
    ties the demo corpus cannot: a year boundary (ISO vs T-SQL week) and
    fractional seconds (Postgres numeric->integer rounding)."""
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "date_probe",
                "select": [
                    "date_probe.id",
                    "date_probe.at",
                    {"expr": {"extract": {"col": "date_probe.at"}, "part": part}, "as": "v"},
                ],
                "order_by": [{"col": "date_probe.id"}],
            }
        )
    )
    assert len(rows) == len(_PROBE_ROWS)
    expected = _DATE_PART_EXPECTATIONS[part]
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["at"])
        assert row["v"] == expected(stamp), f"{part} at {stamp}: got {row['v']}"
        assert isinstance(row["v"], int)
        if part == "second":
            assert row["v"] < 60, "a second field of 60 is not a time"


@pytest.mark.asyncio
async def test_mssql_clock_is_utc_not_the_servers_local_time():
    """`SYSUTCDATETIME()`, never `GETDATE()`/`SYSDATETIME()`.

    T-SQL has no session time zone to pin, so MSSQL's UTC guarantee lives
    entirely in the choice of function — and that choice is invisible unless the
    server's own clock is NOT UTC. Both the compose service and both CI service
    blocks therefore set a non-UTC `TZ`.

    Crucially this test DETECTS the vacuum rather than documenting it: if the
    server's local clock equals its UTC clock, the assertion below cannot
    distinguish `SYSUTCDATETIME()` from `GETDATE()`, so it skips loudly instead
    of reporting a green tick that certifies nothing. A documented vacuum is
    still a vacuum.
    """
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        _MSSQL_URL + f"?driver={_MSSQL_DRIVER.replace(' ', '+')}&TrustServerCertificate=Yes"
    )
    try:
        async with engine.connect() as conn:
            local, utc = (
                await conn.execute(sa.text("SELECT SYSDATETIME(), SYSUTCDATETIME()"))
            ).one()
    finally:
        await engine.dispose()
    if abs((local - utc).total_seconds()) < 60:
        pytest.skip(
            "SQL Server's clock is UTC, so SYSUTCDATETIME() and GETDATE() agree and "
            "this assertion cannot discriminate. Set TZ on the mssql service "
            "(see docker-compose.yml / ci.yml) to make it meaningful."
        )

    _setup()
    result = await StructuredQueryService(connection_id="ms").execute(
        StructuredQuery.model_validate(
            {"from": "orders", "select": [{"expr": {"now": "timestamp"}, "as": "t"}], "limit": 1}
        )
    )
    reading = result.rows[0]["t"]
    if isinstance(reading, str):
        reading = dt.datetime.fromisoformat(reading)
    drift = abs(
        (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - reading.replace(tzinfo=None))
    ).total_seconds()
    assert drift < 300, (
        f"MSSQL clock is {drift}s from UTC — it is reading the server's local "
        "time, not SYSUTCDATETIME()"
    )


@pytest.mark.asyncio
async def test_date_bucket_over_a_non_temporal_column_is_refused_on_both_dialects(
    date_probe_tables,
):
    """item 117, on both real servers.

    Measured before the fix: Postgres raised `function date_trunc(unknown,
    integer) does not exist` while MSSQL happily returned `1900-01-02`. Both are
    now the same typed pre-database rejection, which is the whole point — a
    divergence is closed by making the two agree, not by picking a winner.
    """
    from querygate.core.exceptions import QueryValidationError

    query = StructuredQuery.model_validate(
        {
            "from": "date_probe",
            "select": [{"col": "date_probe.id", "granularity": "day", "as": "bucket"}],
        }
    )
    for connection in ("pg", "ms"):
        with pytest.raises(QueryValidationError, match="date/time column"):
            await StructuredQueryService(connection_id=connection).execute(query)


@pytest.mark.asyncio
async def test_date_bucket_over_a_real_timestamp_still_matches_on_both_dialects(
    date_probe_tables,
):
    """Positive control for the rule above: bucketing a genuine timestamp must
    still work, and still agree across dialects."""
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "date_probe",
                "select": [
                    "date_probe.id",
                    {"col": "date_probe.at", "granularity": "day", "as": "bucket"},
                ],
                "order_by": [{"col": "date_probe.id"}],
            }
        )
    )
    assert len(rows) == len(_PROBE_ROWS)
    assert all(row["bucket"] is not None for row in rows)


# --- item 103: non-equi/range joins + FULL OUTER / CROSS --------------------
#
# FULL OUTER and non-equi joins are universal on PG and MSSQL, so item 103 is
# mechanical translation with no adapter method and no rejection. That claim is
# exactly the kind item 102 proved you cannot make by reading a dialect manual,
# so each form is executed on BOTH live servers and the rows compared.

# One executable join per JoinType. `cross` takes no condition by construction;
# the rest join customers to products on a deliberately mismatched key (customers
# are ids 1-8, products 1-10) so the outer types have unmatched rows to keep.
_JOIN_TYPE_CASES = {
    "inner": {"table": "products", "on": ["customers.id", "products.id"]},
    "left": {"table": "products", "type": "left", "on": ["customers.id", "products.id"]},
    "full": {"table": "products", "type": "full", "on": ["customers.id", "products.id"]},
    "cross": {"table": "products", "type": "cross"},
}


def _setup_allowing_cross() -> None:
    """`_setup()` with the one policy field item 103 adds turned on."""
    _setup()
    set_policy_store(
        PolicyStore(default=Policy(max_select_columns=50, allow_cross_join=True), overrides={})
    )


# The exhaustiveness gate over `_JOIN_TYPE_CASES` deliberately lives in the UNIT
# tier (`tests/unit/test_nonequi_joins.py`), not here — this module is marked
# `postgres_live` AND `mssql_live`, so a gate placed beside the cases it guards
# would only fire in the one CI job that has both servers. That is the same
# reasoning item 102 recorded for its DatePart gate.


@pytest.mark.asyncio
@pytest.mark.parametrize("join_type", sorted(_JOIN_TYPE_CASES))
async def test_join_type_matches_on_both_dialects(join_type):
    _setup_allowing_cross()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": ["customers.id", "products.id"],
                "joins": [_JOIN_TYPE_CASES[join_type]],
                # Ordered by the side that is never NULL here (products 9-10 are
                # the unmatched rows), NOT by customers.id — see
                # `test_outer_join_null_ordering_diverges_and_predates_item_103`.
                "order_by": [{"col": "products.id"}],
                "limit": 200,
            }
        )
    )
    assert rows, f"{join_type} join returned nothing — the comparison is vacuous"


@pytest.mark.asyncio
async def test_full_outer_keeps_the_same_unmatched_rows_on_both_dialects():
    """The rows that distinguish FULL from INNER are the whole point of the type,
    so assert they exist and are identical, not just that the two agree."""
    _setup()
    query = {
        "from": "customers",
        "select": ["customers.id", "products.id"],
        # Ordered by the side that is never NULL here — see
        # `test_outer_join_null_ordering_diverges_and_predates_item_103`.
        "order_by": [{"col": "products.id"}],
        "limit": 200,
    }
    inner = await _assert_same(
        StructuredQuery.model_validate(
            {**query, "joins": [{"table": "products", "on": ["customers.id", "products.id"]}]}
        )
    )
    full = await _assert_same(
        StructuredQuery.model_validate(
            {
                **query,
                "joins": [
                    {"table": "products", "type": "full", "on": ["customers.id", "products.id"]}
                ],
            }
        )
    )
    assert len(full) > len(inner), "FULL OUTER kept nothing extra — nothing is proven"
    assert all(row["id"] is None for row in full if row not in inner)


@pytest.mark.asyncio
async def test_range_join_matches_on_both_dialects():
    """Bar row 16's shape on both real backends: a price-band self-join whose ON
    clause is two inequalities plus item 100 arithmetic."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "products",
                "from_alias": "p",
                "select": ["p.id", "band.id"],
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
                "order_by": [{"col": "p.id"}, {"col": "band.id"}],
                "limit": 200,
            }
        )
    )
    assert len(rows) > len({row["id"] for row in rows}), "no product matched >1 band"


@pytest.mark.asyncio
async def test_cross_join_is_a_real_cartesian_product_on_both_dialects():
    """`ON true` on Postgres and `ON 1 = 1` on MSSQL must be the same product."""
    _setup_allowing_cross()
    counted = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": [{"fn": "count", "col": "*", "as": "n"}],
                "joins": [{"table": "products", "type": "cross"}],
            }
        )
    )
    customers = await _assert_same(
        StructuredQuery.model_validate(
            {"from": "customers", "select": [{"fn": "count", "col": "*", "as": "n"}]}
        )
    )
    products = await _assert_same(
        StructuredQuery.model_validate(
            {"from": "products", "select": [{"fn": "count", "col": "*", "as": "n"}]}
        )
    )
    assert counted[0]["n"] == customers[0]["n"] * products[0]["n"]


@pytest.mark.asyncio
async def test_outer_join_null_ordering_diverges_and_predates_item_103():
    """A recorded divergence, not a regression — and deliberately NOT fixed here.

    An outer join can NULL out the very column the query orders by, and the two
    servers place those NULLs differently: Postgres sorts them LAST on ASC,
    SQL Server sorts them FIRST. So the same AST returns the same row SET in a
    different ORDER on the two backends.

    Item 103 did not introduce this. Measured on both live servers: a plain LEFT
    JOIN — shipped long before item 103 — diverges identically, which is what
    this test asserts. What item 103 changed is only that FULL OUTER makes it
    reachable from a second join type.

    It is left standing rather than papered over because the only in-engine fix
    is `OrderBySpec.nulls`, which item 74 deliberately REJECTS on MSSQL (T-SQL
    has no `NULLS FIRST/LAST`, and synthesizing a CASE-based sort column is the
    exact "don't spoon-feed the agent" line). Changing that is a maintainer
    decision, not a side effect of this item. A caller who needs a deterministic
    order across both backends orders by a non-nullable column — which is what
    the item-103 tests above do.
    """
    _setup()
    query = StructuredQuery.model_validate(
        {
            "from": "products",
            "select": ["products.id", "customers.id"],
            "joins": [
                {"table": "customers", "type": "left", "on": ["products.id", "customers.id"]}
            ],
            "order_by": [{"col": "customers.id"}],
            "limit": 200,
        }
    )
    pg = _rows(await StructuredQueryService(connection_id="pg").execute(query))
    ms = _rows(await StructuredQueryService(connection_id="ms").execute(query))

    assert sorted(r["id"] for r in pg) == sorted(r["id"] for r in ms), "row SETS must match"
    assert pg != ms, "the divergence disappeared — re-check item 74's NULLS posture"
    assert pg[-1]["id_1"] is None, "Postgres is expected to sort NULLs LAST on ASC"
    assert ms[0]["id_1"] is None, "SQL Server is expected to sort NULLs FIRST on ASC"


# --------------------------------------------------------------------------- #
# Set operations (TODO.md item 104)                                            #
# --------------------------------------------------------------------------- #

_ARM_ONE = {
    "from": "orders",
    "select": ["orders.customer_id"],
    "where": {"col": "orders.status", "op": "eq", "value": "completed"},
}
_ARM_TWO = {
    "from": "orders",
    "select": ["orders.customer_id"],
    "where": {"col": "orders.total_amount", "op": "gt", "value": 100},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["union", "intersect", "except"])
async def test_set_operation_matches_on_both_dialects(op):
    """Every operator's DISTINCT form exists on both servers with the same
    keyword, so the identical AST must return the identical rows. Ordered by the
    single projected column so the comparison is not order-dependent."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                **_ARM_ONE,
                "set_op": {"op": op, "arms": [_ARM_TWO]},
                "order_by": [{"col": "customer_id"}],
                "limit": 100,
            }
        )
    )
    assert rows, f"{op} returned no rows — the comparison would be vacuous"


@pytest.mark.asyncio
async def test_union_all_keeps_duplicates_identically_on_both_dialects():
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                **_ARM_ONE,
                "set_op": {"op": "union", "all": True, "arms": [_ARM_ONE]},
                "order_by": [{"col": "customer_id"}],
                "limit": 100,
            }
        )
    )
    assert len(rows) > len({r["customer_id"] for r in rows}), "UNION ALL kept no duplicates"


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["intersect", "except"])
async def test_intersect_all_and_except_all_are_rejected_on_mssql_and_run_on_postgres(op):
    """The one genuine capability gap this item has: Postgres has `INTERSECT ALL`
    and `EXCEPT ALL`; T-SQL has neither. Proven by EXECUTION on both servers —
    Postgres returns rows, MSSQL raises a typed rejection before any SQL is sent.
    A rendering-only assertion could not tell these apart, because SQLAlchemy
    emits `INTERSECT ALL` for the mssql dialect just as happily."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            **_ARM_ONE,
            "set_op": {"op": op, "all": True, "arms": [_ARM_TWO]},
            "order_by": [{"col": "customer_id"}],
            "limit": 100,
        }
    )
    pg = await StructuredQueryService(connection_id="pg").execute(query)
    assert pg.row_count > 0

    from querygate.core.exceptions import QueryValidationError

    with pytest.raises(QueryValidationError, match=f"{op.upper()} ALL is not supported on MSSQL"):
        await StructuredQueryService(connection_id="ms").execute(query)


@pytest.mark.asyncio
async def test_the_compound_limit_is_enforced_on_both_dialects():
    """SQLAlchemy's MSSQL dialect silently DROPS `.limit()` on a CompoundSelect,
    so this is the test that would catch `clamp_limit` — a policy guardrail —
    becoming a no-op on one dialect only. Executed, not rendered: a returned row
    count is the only proof the limit reached the server."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "set_op": {"op": "union", "arms": [{"from": "products", "select": ["products.id"]}]},
            "order_by": [{"col": "id"}],
            "limit": 3,
        }
    )
    for connection in ("pg", "ms"):
        result = await StructuredQueryService(connection_id=connection).execute(query)
        assert result.row_count == 3, f"{connection} returned {result.row_count} rows, not 3"


@pytest.mark.asyncio
async def test_each_arm_carries_its_own_aggregation_on_both_dialects():
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.status", {"fn": "count", "col": "*", "as": "n"}],
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
                "order_by": [{"col": "status"}],
                "limit": 100,
            }
        )
    )
    labels = {r["status"] for r in rows}
    assert "completed" in labels and "US" in labels, labels


@pytest.mark.asyncio
async def test_mismatched_arm_types_are_refused_pre_database_on_both_dialects():
    """The divergence this closes, measured on both live servers BEFORE the fix:

    * arm 1 projecting an integer column against arm 2 projecting a text CAST of
      it — Postgres **errors**; SQL Server **succeeds**, applying data-type
      precedence to convert the varchar side back to int and returning 20 rows;
    * an integer column against a genuine text column — **both** error.

    So the identical AST was a hard failure on one backend and an answer on the
    other, the items 75/82 class this project treats as a defect. Both shapes are
    now one typed pre-database rejection on every dialect, which is what this
    asserts. It is deliberately an INVERSION of the test that previously recorded
    the divergence as an accepted residual, not a deletion of it — the same
    posture item 118 took when it closed the k-anonymity fan-out leak.
    """
    _setup()
    numeric_text = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "set_op": {
                "op": "union",
                "arms": [
                    {
                        "from": "orders",
                        "select": [
                            {"expr": {"cast": {"col": "orders.id"}, "to": "text"}, "as": "id"}
                        ],
                    }
                ],
            },
            "limit": 100,
        }
    )
    non_numeric_text = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "set_op": {"op": "union", "arms": [{"from": "orders", "select": ["orders.status"]}]},
            "limit": 100,
        }
    )
    for query in (numeric_text, non_numeric_text):
        for connection in ("pg", "ms"):
            with pytest.raises(QueryValidationError, match="disagree on the type"):
                await StructuredQueryService(connection_id=connection).execute(query)


@pytest.mark.asyncio
async def test_a_set_operation_inside_an_in_subquery_executes_on_both_dialects():
    """Set operations and subqueries compose, and the composition is advertised in
    the product guide — but until now only `validate_policy` was exercised, never
    the compile or execution path through `_compile_in_subquery` ->
    `_compile_set_operation`."""
    _setup()
    rows = await _assert_same(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": ["orders.id"],
                "where": {
                    "col": "orders.customer_id",
                    "op": "in",
                    "value_subquery": {
                        "from": "customers",
                        "select": ["customers.id"],
                        "where": {"col": "customers.country", "op": "eq", "value": "US"},
                        "set_op": {
                            "op": "union",
                            "arms": [
                                {
                                    "from": "customers",
                                    "select": ["customers.id"],
                                    "where": {
                                        "col": "customers.country",
                                        "op": "eq",
                                        "value": "GB",
                                    },
                                }
                            ],
                        },
                    },
                },
                "order_by": [{"col": "id"}],
                "limit": 100,
            }
        )
    )
    assert rows, "the nested set operation matched nothing — the check would be vacuous"


@pytest.mark.asyncio
async def test_a_mandatory_row_filter_reaches_every_arm_on_both_dialects():
    """The item's headline safety rule, proven by EXECUTION rather than by counting
    occurrences in rendered SQL: with a filter pinned to one country, neither arm
    may return a row from another."""
    _setup()
    set_policy_store(
        PolicyStore(
            default=Policy(
                max_select_columns=50,
                mandatory_row_filters=[
                    MandatoryRowFilter(table="customers", column="country", value="US")
                ],
            ),
            overrides={},
        )
    )
    try:
        query = StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": ["customers.country"],
                "where": {"col": "customers.id", "op": "gt", "value": 0},
                "set_op": {
                    "op": "union",
                    "all": True,
                    "arms": [{"from": "customers", "select": ["customers.country"]}],
                },
                "limit": 100,
            }
        )
        for connection in ("pg", "ms"):
            result = await StructuredQueryService(connection_id=connection).execute(query)
            countries = {row["country"] for row in result.rows}
            assert countries == {"US"}, f"{connection} leaked {countries - {'US'}} past the filter"
    finally:
        set_policy_store(PolicyStore(default=Policy(max_select_columns=50), overrides={}))


# --------------------------------------------------------------------------- #
# Named WITH blocks — ctes (TODO.md item 105)                                  #
# --------------------------------------------------------------------------- #
#
# A `WITH` clause is standard on both backends, so the risk here is not syntax —
# it is the same class item 104 hit, where SQLAlchemy's MSSQL dialect silently
# dropped `.limit()` on a compound SELECT and turned a guardrail into a no-op on
# one backend only. Rendering assertions cannot tell a correct rendering from one
# that merely looks correct, so these EXECUTE and compare rows.


@pytest.mark.asyncio
async def test_cte_aggregate_then_join_matches():
    """The canonical shape: aggregate in a block, join the result to a table."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "totals",
                    "query": {
                        "from": "orders",
                        "select": [
                            "orders.customer_id",
                            {"fn": "sum", "col": "orders.total_amount", "as": "total"},
                        ],
                        "group_by": ["orders.customer_id"],
                    },
                }
            ],
            "from": "customers",
            "joins": [{"table": "totals", "on": ["customers.id", "totals.customer_id"]}],
            "select": ["customers.name", "totals.total"],
            "order_by": [{"col": "customers.name"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert rows, "vacuous - no customer had orders"


@pytest.mark.asyncio
async def test_cte_body_is_not_row_capped_on_either_dialect():
    """The item-104 lesson applied to item 105: a guardrail (or its deliberate
    ABSENCE) has to hold identically on both backends. A grand total computed
    through a block, under a row cap far below the number of orders, must equal
    the total computed directly — on Postgres AND on SQL Server."""
    _setup()
    via_cte = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "all_orders",
                    "query": {"from": "orders", "select": ["orders.total_amount"]},
                }
            ],
            "from": "all_orders",
            "select": [{"fn": "sum", "col": "all_orders.total_amount", "as": "grand"}],
            "limit": 1,
        }
    )
    direct = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [{"fn": "sum", "col": "orders.total_amount", "as": "grand"}],
        }
    )
    through_block = await _assert_same(via_cte)
    straight = await _assert_same(direct)
    assert through_block[0]["grand"] == straight[0]["grand"]
    assert through_block[0]["grand"], "vacuous - the total was zero/None"


@pytest.mark.asyncio
async def test_chained_ctes_match():
    """A block reading an earlier block — two materialization stages in one
    statement, which each planner is free to handle differently."""
    _setup_with_tables(_TABLES)
    set_policy_store(
        PolicyStore(default=Policy(max_select_columns=50, max_subquery_depth=3), overrides={})
    )
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "totals",
                    "query": {
                        "from": "orders",
                        "select": [
                            "orders.customer_id",
                            {"fn": "sum", "col": "orders.total_amount", "as": "total"},
                        ],
                        "group_by": ["orders.customer_id"],
                    },
                },
                {
                    "name": "big",
                    "query": {
                        "from": "totals",
                        "select": ["totals.customer_id", "totals.total"],
                        "where": {"col": "totals.total", "op": "gt", "value": 1},
                    },
                },
            ],
            "from": "big",
            "select": ["big.customer_id", "big.total"],
            "order_by": [{"col": "big.customer_id"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert rows, "vacuous - no customer cleared the threshold"


@pytest.mark.asyncio
async def test_cte_regression_bar_row_3_moving_average_matches():
    """§5 row 3 on both real backends: a window over an AGGREGATED result, which
    needs the aggregate materialized as a block first. This is the query the plan
    recorded as impossible before item 105."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            "ctes": [
                {
                    "name": "daily",
                    "query": {
                        "from": "orders",
                        "select": [
                            {"col": "orders.created_at", "granularity": "day", "as": "day"},
                            {"fn": "count", "col": "*", "as": "n"},
                        ],
                        "group_by": ["day"],
                    },
                }
            ],
            "from": "daily",
            "select": [
                "daily.day",
                "daily.n",
                {
                    "fn": "avg",
                    "arg": {"col": "daily.n"},
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
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert len(rows) > 1, "vacuous - a moving average over one bucket proves nothing"


# --------------------------------------------------------------------------- #
# Correlated / EXISTS / scalar subqueries (TODO.md item 106)                   #
# --------------------------------------------------------------------------- #
#
# The item with the largest safety surface in the plan, and the one whose
# semantics most plausibly diverge: correlated-subquery planning, EXISTS
# short-circuiting, and NOT EXISTS over a NULL-bearing correlated column are all
# places backends historically differ. Rendered SQL cannot tell a correct
# correlation from one that quietly ignores the outer row — both render fine —
# so these execute and compare rows.


@pytest.mark.asyncio
async def test_correlated_exists_and_not_exists_match():
    """EXISTS and NOT EXISTS must partition the outer table identically on both
    backends. Filtered to `cancelled` deliberately: every customer in the seed has
    orders, so an unfiltered partition would be all-vs-none — a split that an
    implementation IGNORING the correlated row would reproduce exactly."""
    _setup()
    seen = []
    for op in ("exists", "not_exists"):
        query = StructuredQuery.model_validate(
            {
                "from": "customers",
                "select": ["customers.id"],
                "where": {
                    "op": op,
                    "exists_subquery": {
                        "from": "orders",
                        "select": ["orders.id"],
                        "correlate": ["customers.id"],
                        "where": {
                            "and": [
                                {
                                    "col": "orders.customer_id",
                                    "op": "eq",
                                    "value_col": "customers.id",
                                },
                                {"col": "orders.status", "op": "eq", "value": "cancelled"},
                            ]
                        },
                    },
                },
                "order_by": [{"col": "customers.id"}],
                "limit": 50,
            }
        )
        seen.append({row["id"] for row in await _assert_same(query)})
    have, lack = seen
    assert have and lack, "vacuous - the partition must be non-trivial on both sides"
    assert have & lack == set()


@pytest.mark.asyncio
async def test_scalar_subquery_comparison_matches():
    """Regression bar row 11 on both real backends: each row compared against an
    aggregate over the whole table, in one statement."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id", "orders.total_amount"],
            "where": {
                "col": "orders.total_amount",
                "op": "gt",
                "value_subquery": {
                    "from": "orders",
                    "select": [{"fn": "avg", "col": "orders.total_amount", "as": "a"}],
                },
            },
            "order_by": [{"col": "orders.id"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert rows, "vacuous - nothing was above average"


@pytest.mark.asyncio
async def test_correlated_scalar_subquery_matches_per_outer_row():
    """The strongest correlation check: each order against ITS OWN customer's
    average, not the global one. A backend (or a compiler) that dropped the
    correlation would silently use the global average — a different, plausible,
    entirely wrong answer."""
    _setup()
    query = StructuredQuery.model_validate(
        {
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
            "order_by": [{"col": "orders.id"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert rows, "vacuous - no order beat its own customer's average"


@pytest.mark.asyncio
async def test_scalar_subquery_in_having_matches():
    """The HAVING position, which needed its own compiler context — comparing one
    aggregate against another."""
    _setup()
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": [
                "orders.customer_id",
                {"fn": "sum", "col": "orders.total_amount", "as": "t"},
            ],
            "group_by": ["orders.customer_id"],
            "having": {
                "col": "t",
                "op": "gt",
                "value_subquery": {
                    "from": "orders",
                    "select": [{"fn": "avg", "col": "orders.total_amount", "as": "a"}],
                },
            },
            "order_by": [{"col": "orders.customer_id"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(query)
    assert rows, "vacuous - no customer's total beat the average order"


@pytest.mark.asyncio
async def test_not_exists_over_a_nullable_correlated_column_matches():
    """The classic NOT EXISTS / NOT IN divergence. `NOT IN` over a set containing
    NULL returns no rows on a standards-following backend, while `NOT EXISTS` is
    NULL-safe. Executing both here records which semantics QueryGate actually has,
    identically on Postgres and SQL Server, rather than assuming."""
    _setup()
    not_exists = StructuredQuery.model_validate(
        {
            "from": "customers",
            "select": ["customers.id"],
            "where": {
                "op": "not_exists",
                "exists_subquery": {
                    "from": "orders",
                    "select": ["orders.id"],
                    "correlate": ["customers.id"],
                    "where": {
                        "and": [
                            {"col": "orders.customer_id", "op": "eq", "value_col": "customers.id"},
                            {"col": "orders.status", "op": "eq", "value": "refunded"},
                        ]
                    },
                },
            },
            "order_by": [{"col": "customers.id"}],
            "limit": 50,
        }
    )
    rows = await _assert_same(not_exists)
    assert rows, "vacuous - every customer had a refunded order"
