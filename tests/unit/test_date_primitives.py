"""Unit tests for item 102's date/time primitives: EXTRACT, now, and date_add.

Three axes, because three different things can go wrong with a date primitive:

1. **AST** — the nodes are closed, typed, and reject an unmodelled part/unit.
2. **Cap** — `max_interval_days` bounds a `date_add`'s magnitude wherever the
   node is nested, and is counted with UPPER-bound unit lengths so a unit whose
   real length varies cannot under-count.
3. **Dialect honesty** — the two parts whose *value* (not spelling) differs
   natively between dialects render the definition QueryGate documents, and the
   one part SQLite genuinely lacks is rejected rather than approximated.

Point 3 is the one a rendering assertion cannot fully settle, so the live
proof lives elsewhere: `tests/integration/test_postgres_date_primitives.py` for
Postgres, and `tests/integration/test_cross_dialect_differential.py` for the
PG-vs-MSSQL value comparison (there is no `test_mssql_date_primitives.py` — the
MSSQL proof is in the differential suite, because what matters for the two
normalized parts is that both servers return the SAME number). What is asserted
here is the *shape* those suites then execute.
"""

from __future__ import annotations

from typing import Dict

import pydantic
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mssql, postgresql, sqlite

from querygate.compiler.dialect_adapters import get_dialect_adapter
from querygate.compiler.sqlalchemy_compiler import _compile_expression
from querygate.core.exceptions import PolicyViolationError, QueryValidationError
from querygate.policy.models import Policy
from querygate.query_ast.models import (
    DateAddExpr,
    DatePart,
    ExtractExpr,
    IntervalUnit,
    NowExpr,
    StructuredQuery,
    interval_magnitude_days,
)
from querygate.validation.policy_validation import validate_policy

pytestmark = pytest.mark.unit

_DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mssql": mssql.dialect(),
    "sqlite": sqlite.dialect(),
}


def _tables() -> Dict[str, sa.Table]:
    metadata = sa.MetaData()
    orders = sa.Table(
        "orders",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime),
        sa.Column("total_amount", sa.Numeric(10, 2)),
    )
    return {"orders": orders}


def _sql(expr, dialect: str) -> str:
    compiled = _compile_expression(expr, _tables(), dialect)
    return str(compiled.compile(dialect=_DIALECTS[dialect], compile_kwargs={"literal_binds": True}))


def _query(expr_payload: dict, **overrides) -> StructuredQuery:
    body = {"from": "orders", "select": [{"expr": expr_payload, "as": "v"}]}
    body.update(overrides)
    return StructuredQuery.model_validate(body)


# --------------------------------------------------------------------------- #
# AST — closed and typed
# --------------------------------------------------------------------------- #
def test_unmodelled_date_part_is_rejected():
    with pytest.raises(pydantic.ValidationError):
        ExtractExpr.model_validate({"extract": {"col": "orders.created_at"}, "part": "fortnight"})


def test_unmodelled_interval_unit_is_rejected():
    with pytest.raises(pydantic.ValidationError):
        DateAddExpr.model_validate(
            {"date_add": {"col": "orders.created_at"}, "unit": "microsecond", "amount": 1}
        )


def test_now_only_accepts_timestamp_or_date():
    with pytest.raises(pydantic.ValidationError):
        NowExpr.model_validate({"now": "epoch"})


@pytest.mark.parametrize(
    "payload",
    [
        {"extract": {"col": "orders.created_at"}, "part": "hour", "tz": "UTC"},
        {"now": "timestamp", "at": "UTC"},
        {"date_add": {"col": "orders.created_at"}, "unit": "day", "amount": 1, "tz": "UTC"},
    ],
)
def test_every_new_node_forbids_extra_fields(payload):
    """`extra="forbid"` on each: an unmodelled key is rejected, never ignored.
    A silently-dropped field is how a caller comes to believe a bound was applied
    that never was."""
    with pytest.raises(pydantic.ValidationError):
        _query(payload)


def test_date_add_amount_must_be_an_integer():
    """A fractional amount would render as a float into an integer interval
    position; reject at the AST rather than let each dialect round differently."""
    with pytest.raises(pydantic.ValidationError):
        DateAddExpr.model_validate(
            {"date_add": {"col": "orders.created_at"}, "unit": "day", "amount": 1.5}
        )


# --------------------------------------------------------------------------- #
# The interval cap
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "unit,amount,expected_days",
    [
        ("day", 7, 7),
        ("day", -7, 7),  # magnitude, not direction
        ("week", 2, 14),
        ("year", 1, 366),  # UPPER bound: a leap year is 366, not 365
        ("month", 1, 31),  # UPPER bound: 31, not 30 or 28
        ("hour", 25, 2),  # rounds UP — 25h reaches into a second day
        ("minute", 1, 1),  # any non-zero sub-day reach counts as a day
        ("second", 0, 0),
        # The two rows below exist because the `("minute", 1, 1)` and
        # `("second", 0, 0)` cases above CANNOT detect a wrong multiplier: ceil
        # division flattens every sub-day magnitude to 1, so `"minute": 1`
        # instead of 60 gives the identical answer and the cap would silently
        # admit a 1,800-day reach. These pick magnitudes where the multiplier
        # is the only thing that decides the result.
        ("minute", 1441, 2),  # 1441 min = 24h01m -> 2 days; at multiplier 1 -> 1
        ("second", 86401, 2),  # 86401 s = 24h00m01s -> 2 days
    ],
)
def test_interval_magnitude_uses_upper_bound_unit_lengths(unit, amount, expected_days):
    node = DateAddExpr(date_add=NowExpr(now="timestamp"), unit=unit, amount=amount)
    assert interval_magnitude_days(node) == expected_days


def test_interval_cap_rejects_an_over_magnitude_shift():
    policy = Policy(max_interval_days=30)
    query = _query({"date_add": {"now": "timestamp"}, "unit": "day", "amount": -31})
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(query, policy, "demo")


def test_interval_cap_allows_a_shift_at_the_boundary():
    policy = Policy(max_interval_days=30)
    validate_policy(
        _query({"date_add": {"now": "timestamp"}, "unit": "day", "amount": -30}), policy, "demo"
    )


def test_interval_cap_cannot_be_dodged_by_choosing_a_bigger_unit():
    """The reason magnitudes are converted to days at all: `2 months` is a
    ~62-day reach, so a 30-day cap must refuse it even though "2" is small."""
    policy = Policy(max_interval_days=30)
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(
            _query({"date_add": {"now": "timestamp"}, "unit": "month", "amount": -2}),
            policy,
            "demo",
        )


def test_interval_cap_reaches_a_date_add_buried_in_an_expression():
    """The cap walks `iter_expression_nodes`, not the select list, so nesting the
    node inside arithmetic inside a CASE cannot escape it."""
    policy = Policy(max_interval_days=30)
    buried = {
        "when": [
            {
                "when": {"col": "orders.id", "op": "gt", "value": 0},
                "then": {"date_add": {"col": "orders.created_at"}, "unit": "year", "amount": 5},
            }
        ]
    }
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(_query(buried), policy, "demo")


def test_interval_cap_reaches_a_date_add_inside_a_where_predicate():
    policy = Policy(max_interval_days=30)
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.created_at",
                "op": "gte",
                "value_expr": {"date_add": {"now": "timestamp"}, "unit": "year", "amount": -5},
            },
        }
    )
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(query, policy, "demo")


def test_interval_cap_reaches_a_date_add_inside_a_subquery():
    """Item 97's rule: a nested scope gets the full per-scope treatment, so a
    subquery is not a place to hide an over-cap magnitude."""
    policy = Policy(max_interval_days=30)
    query = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "col": "orders.id",
                "op": "in",
                "value_subquery": {
                    "from": "orders",
                    "select": [
                        {
                            "expr": {
                                "date_add": {"col": "orders.created_at"},
                                "unit": "year",
                                "amount": 5,
                            },
                            "as": "v",
                        }
                    ],
                },
            },
        }
    )
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(query, policy, "demo")


def test_zero_interval_cap_permits_only_a_no_op_shift():
    """Documented behavior of `max_interval_days=0` — the closest thing to
    switching relative-date arithmetic off for a connection."""
    policy = Policy(max_interval_days=0)
    validate_policy(
        _query({"date_add": {"now": "timestamp"}, "unit": "day", "amount": 0}), policy, "demo"
    )
    with pytest.raises(PolicyViolationError, match="max_interval_days"):
        validate_policy(
            _query({"date_add": {"now": "timestamp"}, "unit": "second", "amount": 1}),
            policy,
            "demo",
        )


@pytest.mark.parametrize("amount", [-1, -5, -10])
def test_default_interval_cap_allows_a_lookback_written_in_years(amount):
    """The default must admit the most NATURAL spelling of the window the docs
    promise, not just an equivalent one in days.

    This is a regression test for a real defect: the default was 3,653 (10 x
    365.3) while the cap counts a year as 366 days, so `{"unit": "year",
    "amount": -10}` converted to 3,660 and was REJECTED by a cap three documents
    described as allowing ~10 years. A caller writing the obvious thing got a
    policy violation. The default is now 10 x 366."""
    validate_policy(
        _query({"date_add": {"now": "timestamp"}, "unit": "year", "amount": amount}),
        Policy(),
        "demo",
    )


def test_default_interval_cap_agrees_with_its_own_unit_arithmetic():
    """Pin the relationship rather than the number: whatever the default is, it
    must equal a whole number of years as `interval_magnitude_days` counts one,
    or the "~N years" the docs promise is again unreachable."""
    default = Policy().max_interval_days
    year = interval_magnitude_days(
        DateAddExpr(date_add=NowExpr(now="timestamp"), unit="year", amount=1)
    )
    assert default % year == 0, (
        f"default {default} is not a whole number of {year}-day years, so the "
        "largest year-expressed lookback it admits is less than it advertises"
    )


def test_new_nodes_count_toward_the_expression_node_budget():
    """Every new member must contribute its size, or it is a cap bypass."""
    policy = Policy(max_expression_nodes=2)
    with pytest.raises(PolicyViolationError, match="expression node count"):
        validate_policy(
            _query(
                {
                    "date_add": {"extract": {"col": "orders.created_at"}, "part": "year"},
                    "unit": "day",
                    "amount": 1,
                }
            ),
            policy,
            "demo",
        )


def test_new_nodes_count_toward_the_expression_depth_cap():
    policy = Policy(max_expression_depth=2)
    with pytest.raises(PolicyViolationError, match="expression nesting depth"):
        validate_policy(
            _query(
                {
                    "date_add": {"extract": {"col": "orders.created_at"}, "part": "year"},
                    "unit": "day",
                    "amount": 1,
                }
            ),
            policy,
            "demo",
        )


# --------------------------------------------------------------------------- #
# Dialect honesty
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("part", ["year", "quarter", "month", "day", "hour", "minute", "second"])
def test_every_ordinary_part_renders_on_every_dialect(part):
    expr = ExtractExpr(extract={"col": "orders.created_at"}, part=part)
    for dialect in _DIALECTS:
        assert _sql(expr, dialect)


def test_mssql_dayofweek_is_datefirst_independent():
    """T-SQL's DATEPART(weekday) is 1-based and moves with `SET DATEFIRST`, so
    passing it through would return a server-config-dependent number. The
    rendered idiom must reference @@DATEFIRST and normalize modulo 7."""
    sql = _sql(ExtractExpr(extract={"col": "orders.created_at"}, part="dayofweek"), "mssql")
    assert "@@DATEFIRST" in sql
    assert "% 7" in sql


def test_mssql_week_uses_iso_week():
    """T-SQL's plain `week` is a different, DATEFIRST-dependent count than the
    ISO week this primitive is defined as."""
    sql = _sql(ExtractExpr(extract={"col": "orders.created_at"}, part="week"), "mssql")
    assert "iso_week" in sql


def test_postgres_extract_is_cast_to_integer():
    """Postgres 14+ returns `numeric` from EXTRACT. Without the cast the same
    AST yields a Decimal here and an int on MSSQL — a silent cross-dialect type
    difference, which is exactly what the differential suite exists to catch."""
    sql = _sql(ExtractExpr(extract={"col": "orders.created_at"}, part="hour"), "postgresql")
    assert "CAST" in sql.upper() and "INTEGER" in sql.upper()


def test_sqlite_rejects_iso_week_rather_than_approximating_it():
    """The reject-don't-emulate posture (item 74): strftime has no ISO-week code
    in the SQLite builds CPython ships, and `%W` is a *different* count. Return
    a typed rejection naming the primitive to use instead."""
    with pytest.raises(QueryValidationError, match="date_bucket"):
        _sql(ExtractExpr(extract={"col": "orders.created_at"}, part="week"), "sqlite")


def test_postgres_date_add_binds_the_amount_rather_than_inlining_it():
    """`make_interval` positional args keep the caller's number a bound
    parameter — nothing caller-derived may reach the statement text."""
    compiled = _compile_expression(
        DateAddExpr(date_add=NowExpr(now="timestamp"), unit="day", amount=-7),
        _tables(),
        "postgresql",
    )
    sql = str(compiled.compile(dialect=_DIALECTS["postgresql"]))
    assert "make_interval" in sql
    assert "-7" not in sql, "the amount must bind, not be inlined into SQL text"


def test_mssql_now_date_truncates_to_a_real_date_type():
    """`now: "date"` means midnight UTC. The generic sa.Date renders DATETIME
    against an unconnected mssql dialect, which would NOT truncate — so the
    concrete mssql.DATE is named. Guards the exact trap item 100's CAST-to-text
    rationale fell into."""
    sql = _sql(NowExpr(now="date"), "mssql")
    # The CAST TARGET specifically — `SYSUTCDATETIME` itself contains the
    # substring "DATETIME", so a naive absence check would pass vacuously.
    cast_target = sql.upper().rsplit(" AS ", 1)[1].rstrip(")")
    assert cast_target == "DATE", f"expected a DATE cast, got {cast_target!r}"


@pytest.mark.parametrize("kind", ["timestamp", "date"])
def test_every_dialect_reads_the_clock_in_utc(kind):
    """MSSQL must not use GETDATE()/SYSDATETIME() (server-local); Postgres and
    SQLite are UTC by construction (the session pin, and SQLite's 'now')."""
    assert "SYSUTCDATETIME" in _sql(NowExpr(now=kind), "mssql").upper()
    assert "GETDATE" not in _sql(NowExpr(now=kind), "mssql").upper()
    assert "now" in _sql(NowExpr(now=kind), "postgresql").lower()


# The ONLY (dialect, part) pairs allowed to be unsupported. A bare
# `except QueryValidationError: pass` would let "every dialect rejects every
# part" pass, and would not notice a NEW gap appearing — which is the drift
# that matters, since a gap is a capability regression for callers.
_DECLARED_PART_GAPS = {("sqlite", "week")}


@pytest.mark.parametrize("part", list(DatePart.__args__))
def test_every_date_part_is_supported_except_the_declared_gaps(part):
    """No part may render into something the adapter never considered, and the
    set of parts a dialect cannot do is pinned rather than merely tolerated.
    Either it produces SQL, or the pair is a declared gap raising a typed
    QueryValidationError — never a KeyError and never a silent passthrough."""
    expr = ExtractExpr(extract={"col": "orders.created_at"}, part=part)
    for dialect in _DIALECTS:
        declared = (dialect, part) in _DECLARED_PART_GAPS
        try:
            assert _sql(expr, dialect)
            rejected = False
        except QueryValidationError:
            rejected = True
        assert rejected == declared, f"{dialect}/{part}: " + (
            "declared as a gap but rendered" if declared else "rejected but not a declared gap"
        )


@pytest.mark.parametrize("unit", list(IntervalUnit.__args__))
def test_every_interval_unit_renders_on_every_dialect(unit):
    """Unlike parts, no dialect has a genuine gap here — all three can shift by
    every unit, so any failure is a missing mapping, not a capability gap."""
    expr = DateAddExpr(date_add=NowExpr(now="timestamp"), unit=unit, amount=1)
    for dialect in _DIALECTS:
        assert _sql(expr, dialect)


def test_every_date_part_has_a_live_both_dialects_case():
    """A part that renders on both dialects but returns a different VALUE on
    each is invisible to every tier except the live differential, so a live case
    is mandatory for every `DatePart`.

    This guard lives here, in the unit tier, rather than beside the cases it
    guards: the differential module is marked `postgres_live` AND `mssql_live`,
    so a gate placed there only fires in the one CI job that has both servers —
    i.e. the check that a new part was not forgotten would itself be skipped in
    every run a developer normally does. It imports the differential suite's own
    expectation table, so there is still exactly one list."""
    import typing

    from tests.integration.test_cross_dialect_differential import _DATE_PART_EXPECTATIONS

    assert set(typing.get_args(DatePart)) == set(_DATE_PART_EXPECTATIONS), (
        "every DatePart needs a live PG+MSSQL case in "
        "tests/integration/test_cross_dialect_differential.py"
    )


def test_adapter_interface_covers_every_dialect():
    """A dialect that forgot one of the three new methods would be an abstract
    class and fail to instantiate; assert the registry hands back real ones."""
    for dialect in _DIALECTS:
        adapter = get_dialect_adapter(dialect)
        assert callable(adapter.extract_part)
        assert callable(adapter.current_timestamp)
        assert callable(adapter.date_add)
