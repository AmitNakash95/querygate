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
