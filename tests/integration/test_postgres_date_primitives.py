"""Real-Postgres execution of item 102's date/time primitives.

The SQLite end-to-end suite (`test_date_primitives_end_to_end.py`) proves the
semantics and the cross-dialect suite proves PG/MSSQL agree. What is left, and
what can ONLY be shown against a live Postgres, is the timezone decision:

**Postgres resolves `EXTRACT`, `date_trunc` and every timestamp<->timestamptz
conversion against the SESSION `TimeZone`.** QueryGate never set one, so those
answers used to follow whatever zone the server happened to be configured for —
the same query returning different values on two deployments. The 2026-07-26
Decision Log chose UTC, and `PostgresSessionAdapter.apply_session_guardrails`
pins it. That pin is invisible to every rendering assertion and to any test run
against a server that is already UTC (which the demo container is), so the test
below deliberately configures the role's default zone to a NON-UTC one first.
Without the pin these tests fail; that was verified by removing the line.

Also covered here: `make_interval`'s bound parameter really binds. Postgres
defines only `interval * double precision`, so the shorter
`amount * interval '1 day'` form would have resolved the caller's integer to a
float through operator inference — `make_interval` puts it in a typed integer
position instead, and only a live server proves the parameter round-trips.

Needs a real Postgres — run `make compose-up` first, then
`make test-postgres-live` (or `poetry run pytest -m postgres_live`).
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from querygate.connections.engine import reset_engines
from querygate.connections.models import ConnectionProfile
from querygate.connections.registry import ConnectionRegistry, set_registry
from querygate.execution.service import StructuredQueryService
from querygate.policy.loader import PolicyStore, set_policy_store
from querygate.policy.models import Policy
from querygate.query_ast.models import StructuredQuery

pytestmark = [pytest.mark.integration, pytest.mark.real_db, pytest.mark.postgres_live]

_CONNECTION_STRING = "postgresql+asyncpg://querygate:querygate@localhost:5433/querygate_demo"

# UTC-09:30 — a half-hour offset on purpose: a whole-hour zone can coincide with
# a plausible off-by-N bug, while :30 cannot be produced by anything but the
# server's own zone being applied.
_NON_UTC_ZONE = "Pacific/Marquesas"


def _use_connection(known_tables) -> None:
    set_registry(
        ConnectionRegistry(
            {
                "demo": ConnectionProfile(
                    id="demo",
                    dialect="postgresql",
                    connection_string=_CONNECTION_STRING,
                    known_tables=known_tables,
                )
            }
        )
    )
    set_policy_store(PolicyStore(default=Policy(), overrides={}))
    reset_engines()


@pytest.fixture
async def tz_probe_table():
    """A table with a `timestamptz` column (the demo schema's are naive) holding
    one row at exactly 12:00 UTC, plus a role default of a non-UTC zone.

    Both are torn down afterwards, including on failure — leaving the role's
    TimeZone set would silently change every other postgres_live test.
    """
    engine = create_async_engine(_CONNECTION_STRING)
    async with engine.begin() as conn:
        # Reset FIRST, unconditionally. `finally` below covers an assertion
        # failure but not a SIGKILL or a CI step timeout, and this setting lives
        # in the server's catalog — a killed run would leave every subsequent
        # postgres_live test executing against a UTC-09:30 server, which is
        # exactly the configuration this item proves changes results.
        await conn.execute(sa.text("ALTER ROLE querygate RESET TimeZone"))
        await conn.execute(sa.text("DROP TABLE IF EXISTS tz_probe"))
        await conn.execute(sa.text("CREATE TABLE tz_probe (id int, at timestamptz)"))
        await conn.execute(
            sa.text("INSERT INTO tz_probe VALUES (1, TIMESTAMPTZ '2026-03-15 12:00:00+00')")
        )
        await conn.execute(sa.text(f"ALTER ROLE querygate SET TimeZone TO '{_NON_UTC_ZONE}'"))
    await engine.dispose()
    try:
        yield
    finally:
        engine = create_async_engine(_CONNECTION_STRING)
        async with engine.begin() as conn:
            await conn.execute(sa.text("ALTER ROLE querygate RESET TimeZone"))
            await conn.execute(sa.text("DROP TABLE IF EXISTS tz_probe"))
        await engine.dispose()
        reset_engines()


@pytest.mark.asyncio
async def test_extract_is_utc_even_when_the_server_zone_is_not(tz_probe_table):
    """The headline: a timestamptz stored at 12:00 UTC must EXTRACT hour 12,
    not the 02:30 the server's own zone would give.

    This is the assertion the UTC session pin exists for. With the pin removed
    it returns 2 — verified by deliberately removing the line.
    """
    _use_connection(["tz_probe"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "tz_probe",
                "select": [
                    {"expr": {"extract": {"col": "tz_probe.at"}, "part": "hour"}, "as": "hr"},
                    {"expr": {"extract": {"col": "tz_probe.at"}, "part": "day"}, "as": "day"},
                ],
            }
        )
    )
    row = result.rows[0]
    assert row["hr"] == 12, f"expected the UTC hour, got {row['hr']} (the server-local hour)"
    assert row["day"] == 15


@pytest.mark.asyncio
async def test_now_compared_against_a_naive_column_uses_utc(tz_probe_table):
    """`now()` alone does NOT prove the pin: it returns a timestamptz, an
    absolute instant that asyncpg decodes as UTC whatever the session zone is.
    (An earlier version of this test asserted exactly that and stayed green with
    the pin removed — a test named for a guarantee it did not exercise.)

    The conversion that genuinely follows the session `TimeZone` is
    timestamptz -> naive `timestamp`. Casting `now()` to a naive timestamp and
    comparing it to the process's own UTC clock is therefore discriminating:
    unpinned, the reading comes back 9.5 hours off."""
    _use_connection(["tz_probe"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "tz_probe",
                "select": [
                    {
                        "expr": {"cast": {"now": "timestamp"}, "to": "timestamp"},
                        "as": "naive_now",
                    }
                ],
            }
        )
    )
    reading = result.rows[0]["naive_now"]
    assert reading.tzinfo is None, "expected a naive timestamp for this assertion to mean anything"
    drift = abs((dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - reading).total_seconds())
    assert drift < 300, (
        f"now() rendered {drift}s from UTC when converted to a naive timestamp — "
        "the session TimeZone is not pinned to UTC"
    )


@pytest.mark.asyncio
async def test_now_date_is_todays_utc_date(tz_probe_table):
    """`now: "date"` executed against a real server — previously it was proven
    only on SQLite and by inspecting the SQL text of an *unconnected* dialect,
    which is the weakest tier this repo recognises.

    Honest about its own power: this catches the pin only during the 9.5 hours
    a day when UTC and UTC-09:30 disagree on the date, so it is an
    execution/round-trip test, not the pin's proof. The pin's proof is
    `test_extract_is_utc_even_when_the_server_zone_is_not` above, which is
    deterministic."""
    _use_connection(["tz_probe"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {"from": "tz_probe", "select": [{"expr": {"now": "date"}, "as": "d"}]}
        )
    )
    today = dt.datetime.now(dt.timezone.utc).date()
    assert result.rows[0]["d"] == today


@pytest.mark.asyncio
async def test_date_bucket_is_deterministic_under_the_pin(tz_probe_table):
    """A pre-existing latent nondeterminism this item's pin also closes:
    `date_trunc` over a timestamptz used the server's zone, so day-bucketing the
    same row gave a different day on a non-UTC deployment. 12:00 UTC buckets to
    the 15th under UTC; under UTC-09:30 it would bucket to 02:30 on the 15th and
    a row a few hours earlier would land on the 14th."""
    _use_connection(["tz_probe"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "tz_probe",
                "select": [{"col": "tz_probe.at", "granularity": "day", "as": "bucket"}],
            }
        )
    )
    bucket = result.rows[0]["bucket"]
    assert bucket.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-03-15 00:00"


@pytest.fixture
async def fractional_seconds_table():
    """One row whose timestamp carries a fractional second — the case the demo
    seed cannot express (every seeded `created_at` is exactly midnight, so
    hour/minute/second are 0 everywhere and any mapping error is invisible)."""
    engine = create_async_engine(_CONNECTION_STRING)
    async with engine.begin() as conn:
        await conn.execute(sa.text("DROP TABLE IF EXISTS frac_probe"))
        await conn.execute(sa.text("CREATE TABLE frac_probe (id int, at timestamp)"))
        await conn.execute(
            sa.text(
                "INSERT INTO frac_probe VALUES "
                "(1, TIMESTAMP '2025-01-01 10:20:30.6'), "
                "(2, TIMESTAMP '2025-01-01 13:45:59.7')"
            )
        )
    await engine.dispose()
    try:
        yield
    finally:
        engine = create_async_engine(_CONNECTION_STRING)
        async with engine.begin() as conn:
            await conn.execute(sa.text("DROP TABLE IF EXISTS frac_probe"))
        await engine.dispose()
        reset_engines()


@pytest.mark.asyncio
async def test_extract_second_truncates_rather_than_rounds(fractional_seconds_table):
    """Postgres's `EXTRACT(second …)` returns `numeric` INCLUDING the fraction,
    and `numeric -> integer` ROUNDS HALF AWAY FROM ZERO. So a plain cast turns
    30.6 into 31 and — worse — **59.7 into 60**, a value no clock produces and
    that neither MSSQL's `DATEPART` nor SQLite's `strftime('%S')` can return
    (both truncate).

    That breaks this item's stated contract twice over: "every part returns an
    INTEGER on every dialect, with one definition each". The adapter floors
    before casting. A no-op for every integral part; load-bearing for `second`.
    """
    _use_connection(["frac_probe"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "frac_probe",
                "select": [
                    "frac_probe.id",
                    {"expr": {"extract": {"col": "frac_probe.at"}, "part": "second"}, "as": "sec"},
                    {
                        "expr": {"extract": {"col": "frac_probe.at"}, "part": "minute"},
                        "as": "minute",
                    },
                    {"expr": {"extract": {"col": "frac_probe.at"}, "part": "hour"}, "as": "hour"},
                ],
                "order_by": [{"col": "frac_probe.id"}],
            }
        )
    )
    by_id = {row["id"]: row for row in result.rows}
    assert by_id[1]["sec"] == 30, "30.6 seconds must truncate to 30, not round to 31"
    assert by_id[2]["sec"] == 59, "59.7 seconds must truncate to 59 — 60 is not a second"
    # The same row pins hour/minute against distinct non-zero values, which the
    # all-midnight demo corpus cannot do.
    assert (by_id[1]["hour"], by_id[1]["minute"]) == (10, 20)
    assert (by_id[2]["hour"], by_id[2]["minute"]) == (13, 45)


@pytest.mark.asyncio
async def test_make_interval_binds_the_amount_and_shifts_correctly():
    """`make_interval` over a bound integer parameter really executes — the
    reason it was chosen over `amount * interval '1 day'`, whose only Postgres
    operator is `interval * double precision`."""
    _use_connection(["customers", "orders", "order_items"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    "orders.created_at",
                    {
                        "expr": {
                            "date_add": {"col": "orders.created_at"},
                            "unit": "day",
                            "amount": -7,
                        },
                        "as": "shifted",
                    },
                ],
                "limit": 5,
            }
        )
    )
    assert result.rows
    for row in result.rows:
        assert row["shifted"] == row["created_at"] - dt.timedelta(days=7)


@pytest.mark.asyncio
async def test_every_interval_unit_executes_on_real_postgres():
    """Each unit fills a different `make_interval` position; a mis-mapped
    position would shift by the wrong field and is invisible to a render test."""
    _use_connection(["customers", "orders", "order_items"])
    service = StructuredQueryService(connection_id="demo")
    base = None
    for unit, delta in (
        ("day", dt.timedelta(days=-3)),
        ("week", dt.timedelta(weeks=-3)),
        ("hour", dt.timedelta(hours=-3)),
        ("minute", dt.timedelta(minutes=-3)),
        ("second", dt.timedelta(seconds=-3)),
    ):
        result = await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": [
                        "orders.created_at",
                        {
                            "expr": {
                                "date_add": {"col": "orders.created_at"},
                                "unit": unit,
                                "amount": -3,
                            },
                            "as": "shifted",
                        },
                    ],
                    "order_by": [{"col": "orders.id"}],
                    "limit": 1,
                }
            )
        )
        row = result.rows[0]
        assert row["shifted"] == row["created_at"] + delta, f"{unit} shifted by the wrong field"
        base = row["created_at"]

    # Calendar units separately: not fixed-length, so asserted as calendar math.
    for unit, expected in (("year", (base.year - 3, base.month)), ("month", None)):
        result = await service.execute(
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": [
                        {
                            "expr": {
                                "date_add": {"col": "orders.created_at"},
                                "unit": unit,
                                "amount": -3,
                            },
                            "as": "shifted",
                        }
                    ],
                    "order_by": [{"col": "orders.id"}],
                    "limit": 1,
                }
            )
        )
        shifted = result.rows[0]["shifted"]
        if expected is not None:
            assert (shifted.year, shifted.month) == expected
        else:
            assert shifted < base and (base - shifted).days >= 89


@pytest.mark.asyncio
async def test_postgres_extract_returns_a_python_int_not_a_decimal():
    """Postgres 14+ returns `numeric` from EXTRACT, which asyncpg surfaces as a
    `Decimal`. The adapter's integer cast is what makes the primitive's declared
    "returns an integer" contract true here rather than dialect-dependent."""
    _use_connection(["customers", "orders", "order_items"])
    result = await StructuredQueryService(connection_id="demo").execute(
        StructuredQuery.model_validate(
            {
                "from": "orders",
                "select": [
                    {"expr": {"extract": {"col": "orders.created_at"}, "part": "year"}, "as": "y"}
                ],
                "limit": 1,
            }
        )
    )
    value = result.rows[0]["y"]
    assert isinstance(value, int) and not isinstance(value, bool), f"got {type(value).__name__}"


@pytest.mark.asyncio
async def test_over_cap_interval_never_reaches_the_server():
    """The cap is a pre-DB rejection (policy validation runs before any
    connection is opened), so an absurd magnitude is refused rather than
    becoming a `timestamp out of range` error from Postgres."""
    from querygate.core.exceptions import PolicyViolationError

    _use_connection(["customers", "orders", "order_items"])
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        await StructuredQueryService(connection_id="demo").execute(
            StructuredQuery.model_validate(
                {
                    "from": "orders",
                    "select": ["orders.id"],
                    "where": {
                        "col": "orders.created_at",
                        "op": "gte",
                        "value_expr": {
                            "date_add": {"now": "timestamp"},
                            "unit": "year",
                            "amount": -100000,
                        },
                    },
                }
            )
        )
