"""End-to-end execution of item 102's date/time primitives through the pipeline.

Rendering tests prove the SQL *text*; these prove the primitives return the
right **values** against a real (SQLite) database via the one request pipeline.
Ground truth is derived from a second query through the SAME pipeline wherever
possible, so every guardrail applies on both sides and the comparison isolates
the date primitive.

Row 12 of ENGINE_EXPRESSIVENESS_PLAN.md §5's canonical regression bar — "orders
in the last 7 days" — is the last test here: the wall this item exists to
remove, expressed without the caller computing a timestamp literal.

Note on `week`: SQLite genuinely has no ISO-week function, so this suite cannot
cover it and the adapter rejects it here (asserted in
`tests/unit/test_date_primitives.py`). ISO-week parity between the two real
dialects is proven in `tests/integration/test_cross_dialect_differential.py`.
"""

from __future__ import annotations

import datetime as dt

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.integration

_BASE_URL = "http://localhost"


async def _rows(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        resp = await client.post("/api/v1/demo/query", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["rows"]


async def _post(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE_URL) as client:
        return await client.post("/api/v1/demo/query", json=body)


# Every seeded `orders.created_at` is exactly midnight, so extracting hour,
# minute or second from the raw column asserts `0 == 0` — three mappings could
# be swapped for each other and every assertion would still pass. Shifting the
# column by a distinct amount per unit first makes each field a different
# non-zero number, so a mis-mapped part fails. The shift is `date_add`, already
# proven independently below, so this does not make one primitive's correctness
# depend on an unproven other.
_TIME_OF_DAY_SHIFT = [
    {"date_add": {"col": "orders.created_at"}, "unit": "hour", "amount": 13},
    {"unit": "minute", "amount": 47},
    {"unit": "second", "amount": 29},
]


def _shifted_created_at() -> dict:
    expr = _TIME_OF_DAY_SHIFT[0]
    for step in _TIME_OF_DAY_SHIFT[1:]:
        expr = {"date_add": expr, **step}
    return expr


@pytest.mark.parametrize(
    "part,attr,expected",
    [
        ("year", "year", None),
        ("month", "month", None),
        ("day", "day", None),
        # Distinct non-zero values, so hour/minute/second cannot be confused
        # with each other or with zero.
        ("hour", "hour", 13),
        ("minute", "minute", 47),
        ("second", "second", 29),
    ],
)
@pytest.mark.asyncio
async def test_extract_matches_the_stored_timestamp(sqlite_app, part, attr, expected):
    """Each part is compared against the same row's raw timestamp, parsed
    client-side — so a part that silently returned a *different* field (or a
    string instead of an integer) fails, not just one that errors."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                {"expr": _shifted_created_at(), "as": "moment"},
                {"expr": {"extract": _shifted_created_at(), "part": part}, "as": "v"},
            ],
            "order_by": [{"col": "orders.id", "dir": "asc"}],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["moment"])
        assert row["v"] == getattr(stamp, attr)
        assert isinstance(row["v"], int), "extract must yield an integer, not a string"
        if expected is not None:
            # Belt and braces: pin the literal value too, so the test cannot be
            # satisfied by both sides being wrong in the same way.
            assert row["v"] == expected


@pytest.mark.asyncio
async def test_extract_dayofweek_is_sunday_zero(sqlite_app):
    """The documented cross-dialect definition: 0=Sunday..6=Saturday. Python's
    `weekday()` is Monday=0, so the conversion here is deliberate and is what
    pins the numbering rather than restating the implementation."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.created_at",
                {"expr": {"extract": {"col": "orders.created_at"}, "part": "dayofweek"}, "as": "v"},
            ],
            "limit": 10,
        },
    )
    assert rows
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["created_at"])
        assert row["v"] == (stamp.weekday() + 1) % 7


@pytest.mark.asyncio
async def test_extract_quarter_is_an_integer_one_to_four(sqlite_app):
    """SQLite has no quarter field, so the adapter computes it from the month —
    with a truncating cast, because SQLAlchemy's `/` is TRUE division and month
    2 would otherwise yield 1.33 instead of quarter 1."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.created_at",
                {"expr": {"extract": {"col": "orders.created_at"}, "part": "quarter"}, "as": "v"},
            ],
            "limit": 10,
        },
    )
    assert rows
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["created_at"])
        assert row["v"] == (stamp.month - 1) // 3 + 1
        assert isinstance(row["v"], int), "quarter must be a whole number, not a float"


@pytest.mark.asyncio
async def test_extract_dayofyear_matches(sqlite_app):
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.created_at",
                {"expr": {"extract": {"col": "orders.created_at"}, "part": "dayofyear"}, "as": "v"},
            ],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        stamp = dt.datetime.fromisoformat(row["created_at"])
        assert row["v"] == stamp.timetuple().tm_yday


@pytest.mark.asyncio
async def test_now_reads_a_clock_close_to_the_test_process(sqlite_app):
    """`now` is UTC on every dialect. Comparing against the test process's own
    UTC clock proves the reading is UTC, not local — a local-time reading would
    be off by the machine's offset (and would pass a mere "is a timestamp"
    check)."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [{"expr": {"now": "timestamp"}, "as": "now_ts"}],
            "limit": 1,
        },
    )
    reading = dt.datetime.fromisoformat(rows[0]["now_ts"])
    drift = abs((dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - reading).total_seconds())
    assert drift < 300, f"clock reading is {drift}s from UTC now — not a UTC reading"


@pytest.mark.asyncio
async def test_now_date_is_midnight_today(sqlite_app):
    rows = await _rows(
        sqlite_app,
        {"from": "orders", "select": [{"expr": {"now": "date"}, "as": "d"}], "limit": 1},
    )
    # Either today's or yesterday's UTC date: the DB reads its clock a moment
    # before Python reads its own, so a midnight rollover between the two is a
    # real (if rare) outcome, not a defect worth failing CI over.
    now = dt.datetime.now(dt.timezone.utc)
    acceptable = {
        now.strftime("%Y-%m-%d"),
        (now - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
    }
    assert rows[0]["d"][:10] in acceptable


@pytest.mark.parametrize(
    "unit,amount,delta",
    [
        ("day", -7, dt.timedelta(days=-7)),
        ("day", 3, dt.timedelta(days=3)),
        ("week", -2, dt.timedelta(weeks=-2)),
        ("hour", -36, dt.timedelta(hours=-36)),
        ("minute", 90, dt.timedelta(minutes=90)),
        ("second", -45, dt.timedelta(seconds=-45)),
    ],
)
@pytest.mark.asyncio
async def test_date_add_shifts_a_column_by_the_exact_interval(sqlite_app, unit, amount, delta):
    """Fixed-length units are compared to an exact `timedelta` — so a shift that
    landed on the wrong unit (days for hours, say) fails rather than merely
    "changed the value". Year/month are excluded here precisely because they are
    NOT fixed-length; they are covered separately below."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.created_at",
                {
                    "expr": {
                        "date_add": {"col": "orders.created_at"},
                        "unit": unit,
                        "amount": amount,
                    },
                    "as": "shifted",
                },
            ],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        original = dt.datetime.fromisoformat(row["created_at"])
        shifted = dt.datetime.fromisoformat(row["shifted"])
        assert shifted == original + delta


@pytest.mark.asyncio
async def test_date_add_by_calendar_units_lands_on_the_calendar_date(sqlite_app):
    """Calendar units are not fixed-length, so they are asserted as calendar
    arithmetic: +1 year keeps the month and day, +1 month advances the month."""
    rows = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                "orders.created_at",
                {
                    "expr": {"date_add": {"col": "orders.created_at"}, "unit": "year", "amount": 1},
                    "as": "next_year",
                },
                {
                    "expr": {
                        "date_add": {"col": "orders.created_at"},
                        "unit": "month",
                        "amount": 1,
                    },
                    "as": "next_month",
                },
            ],
            "limit": 5,
        },
    )
    assert rows
    for row in rows:
        original = dt.datetime.fromisoformat(row["created_at"])
        next_year = dt.datetime.fromisoformat(row["next_year"])
        next_month = dt.datetime.fromisoformat(row["next_month"])
        assert (next_year.year, next_year.month, next_year.day) == (
            original.year + 1,
            original.month,
            original.day,
        )
        expected_month = original.month % 12 + 1
        assert next_month.month == expected_month


@pytest.mark.asyncio
async def test_extract_is_groupable_via_a_projected_alias(sqlite_app):
    """The composition recipe the guide documents: a computed GROUP BY key is
    expressed by projecting the expression with an alias and grouping on it.
    Proves EXTRACT participates in the existing alias route rather than needing
    `group_by` to accept an inline expression."""
    grouped = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                {"expr": {"extract": {"col": "orders.created_at"}, "part": "month"}, "as": "mth"},
                {"fn": "count", "col": "orders.id", "as": "n"},
            ],
            "group_by": ["mth"],
        },
    )
    assert grouped
    raw = await _rows(sqlite_app, {"from": "orders", "select": ["orders.created_at"], "limit": 100})
    expected: dict = {}
    for row in raw:
        month = dt.datetime.fromisoformat(row["created_at"]).month
        expected[month] = expected.get(month, 0) + 1
    assert {row["mth"]: row["n"] for row in grouped} == expected


@pytest.mark.asyncio
async def test_bar_row_12_orders_in_a_relative_window_without_a_literal(sqlite_app):
    """Canonical bar row 12: "orders in the last N days" with NO caller-computed
    timestamp literal anywhere in the request. Ground truth is the same window
    computed client-side from the raw rows.

    The corpus is seeded in the past, so a wide window is used to make the
    assertion meaningful (a 7-day window over this data is legitimately empty);
    the point under test is that the relative bound is applied at all, and
    correctly — the narrow-window case is pinned by the complementary assertion
    that a 1-day window returns strictly fewer rows."""
    window_days = 3650
    filtered = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [{"fn": "count", "col": "orders.id", "as": "n"}],
            "where": {
                "col": "orders.created_at",
                "op": "gte",
                "value_expr": {
                    "date_add": {"now": "timestamp"},
                    "unit": "day",
                    "amount": -window_days,
                },
            },
        },
    )
    raw = await _rows(sqlite_app, {"from": "orders", "select": ["orders.created_at"], "limit": 100})
    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(days=window_days)
    expected = sum(1 for r in raw if dt.datetime.fromisoformat(r["created_at"]) >= cutoff)
    assert filtered[0]["n"] == expected

    narrow = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [{"fn": "count", "col": "orders.id", "as": "n"}],
            "where": {
                "col": "orders.created_at",
                "op": "gte",
                "value_expr": {"date_add": {"now": "timestamp"}, "unit": "day", "amount": -1},
            },
        },
    )
    assert narrow[0]["n"] < filtered[0]["n"], "the relative bound is not actually being applied"

    # The two windows above are "everything" and "nothing", which a cutoff wrong
    # by years would still satisfy. This third one is sized to fall BETWEEN the
    # seeded rows, so it only passes if the cutoff lands where it should.
    stamps = sorted(dt.datetime.fromisoformat(r["created_at"]) for r in raw)
    midpoint = stamps[len(stamps) // 2]
    bisecting_days = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - midpoint).days
    bisecting = await _rows(
        sqlite_app,
        {
            "from": "orders",
            "select": [{"fn": "count", "col": "orders.id", "as": "n"}],
            "where": {
                "col": "orders.created_at",
                "op": "gte",
                "value_expr": {
                    "date_add": {"now": "timestamp"},
                    "unit": "day",
                    "amount": -bisecting_days,
                },
            },
        },
    )
    cutoff = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(
        days=bisecting_days
    )
    expected_bisect = sum(1 for stamp in stamps if stamp >= cutoff)
    assert bisecting[0]["n"] == expected_bisect
    assert 0 < bisecting[0]["n"] < filtered[0]["n"], "the window must actually partition the data"


@pytest.mark.asyncio
async def test_over_cap_interval_is_refused_by_the_api(sqlite_app):
    """The cap is enforced on the real request path, not only in unit tests —
    and it is a clean typed rejection, not a server error."""
    resp = await _post(
        sqlite_app,
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.created_at",
                "op": "gte",
                "value_expr": {"date_add": {"now": "timestamp"}, "unit": "year", "amount": -100},
            },
        },
    )
    assert resp.status_code == 422, resp.text
    assert "max_interval_days" in resp.text


@pytest.mark.parametrize(
    "payload",
    [
        {"extract": {"col": "orders.id"}, "part": "hour"},
        {"date_add": {"col": "orders.id"}, "unit": "day", "amount": 1},
    ],
)
@pytest.mark.asyncio
async def test_a_date_primitive_over_a_non_temporal_column_is_refused(sqlite_app, payload):
    """Measured cross-dialect divergence, closed at validation.

    With an INTEGER operand against live servers: Postgres ERRORS on both
    `EXTRACT(hour FROM id)` and the day shift, while MSSQL silently returns `0`
    and `1900-01-03` respectively — T-SQL implicitly converts an int to a
    datetime counted from 1900-01-01. The identical AST was therefore a hard
    failure on one backend and a plausible-looking wrong answer on the other.
    Now it is one typed rejection, before any database is touched.
    """
    resp = await _post(
        sqlite_app,
        {"from": "orders", "select": [{"expr": payload, "as": "v"}]},
    )
    assert resp.status_code == 422, resp.text
    assert "date/time column" in resp.text


@pytest.mark.asyncio
async def test_validation_stands_aside_for_a_computed_operand(sqlite_app):
    """The rule rejects what is known-wrong, not what is merely computed.

    The operand here is `orders.id` — genuinely NON-temporal, so the bare-column
    form is rejected by the test above. Wrapped in a cast it is no longer a bare
    column, has no reflected type to check, and validation stands aside. An
    earlier version of this test cast `created_at`, which is already temporal —
    so it passed whether or not the escape-hatch branch existed, proving nothing
    about the gate its name describes.

    Deliberately asserts only that validation does not reject: an integer cast to
    a timestamp is a meaningless VALUE, and the point is the boundary, not the
    result.
    """
    resp = await _post(
        sqlite_app,
        {
            "from": "orders",
            "select": [
                {
                    "expr": {
                        "extract": {"cast": {"col": "orders.id"}, "to": "timestamp"},
                        "part": "year",
                    },
                    "as": "v",
                }
            ],
            "limit": 1,
        },
    )
    assert "date/time column" not in resp.text, "validation should not reject a computed operand"
