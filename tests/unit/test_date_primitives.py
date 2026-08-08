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
from sqlalchemy.dialects import mssql, mysql, postgresql, sqlite

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

# `sqlalchemy_bigquery` is a dev-only dependency (TODO.md item 19 phase 3,
# same reasoning as `snowflake-sqlalchemy`) — `importorskip` so this module
# still collects cleanly in a hypothetical main-dependencies-only
# environment, matching test_dialect_adapters.py's pattern.
_sqlalchemy_bigquery = pytest.importorskip("sqlalchemy_bigquery")

_DIALECTS = {
    "postgresql": postgresql.dialect(),
    "mssql": mssql.dialect(),
    "mysql": mysql.dialect(),
    "sqlite": sqlite.dialect(),
    # BigQuery (TODO.md item 19 phase 3) joins the shared enum-driven suite
    # from the start — Snowflake (phase 2) did NOT get folded in here
    # (2026-08-06 architecture-boundary-reviewer finding on that item,
    # flagged as a gap not to repeat). Two different operand types get
    # exercised by this fixture depending on the test: `orders.created_at`
    # below is a plain `sa.DateTime`, which `_bq_temporal_kind` classifies as
    # BigQuery's DATETIME kind — the one BigQuery temporal kind with no
    # genuine date_add unit gap (DATETIME_ADD supports the full IntervalUnit
    # range) — used by most tests in this file (e.g.
    # `test_every_ordinary_part_renders_on_every_dialect`). But
    # `test_every_interval_unit_renders_on_every_dialect` below builds its
    # `date_add` expression over `NowExpr(now="timestamp")` instead, which
    # `BigQueryDialectAdapter.current_timestamp` renders as a TIMESTAMP —
    # THAT kind genuinely lacks week/month/year (TIMESTAMP_ADD's real gap),
    # which is why that one test needs its own `_DECLARED_UNIT_GAPS` set
    # further down rather than being gap-free like the DATETIME case. Its
    # DATE-only and TIMESTAMP-only `date_add` unit gaps are covered more
    # exhaustively in test_dialect_adapters.py's `TestBigQueryDateAddTypeGaps`
    # against concretely DATE/TIMESTAMP-typed columns.
    "bigquery": _sqlalchemy_bigquery.BigQueryDialect(),
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


@pytest.mark.parametrize("dialect", ["mysql", "bigquery"])
def test_two_date_adds_in_one_query_do_not_collide_on_the_same_bind_name(dialect):
    """2026-08-07 security-invariant-reviewer finding: MySQL's and BigQuery's
    `date_add` both build their INTERVAL clause with `sa.text("INTERVAL :amt
    ...").bindparams(amt=amount)` — a bare, non-unique bindparam name. Two
    `date_add` calls compiled into the SAME statement (e.g. a two-sided
    window filter, `col > date_add(now(), 'day', -30) AND col < date_add(
    now(), 'day', -1)`) previously rendered two `:amt` placeholders sharing
    ONE bound value — the LAST amount silently won and the first was lost,
    with no error raised. This is reachable today on MySQL (already
    shipped, live-tested) — not a guardrail bypass (`max_interval_days` is
    checked pre-compile, so no cap is defeated), but a silent-wrong-results
    correctness bug: the query above would use -1 for BOTH bounds instead
    of -30/-1, returning the wrong row set with no signal to the caller.
    Fixed with `sa.bindparam("amt", value=amount, unique=True)` at both call
    sites. This test fails today (before the fix) for the reason above."""
    lower = DateAddExpr(date_add=NowExpr(now="timestamp"), unit="day", amount=-30)
    upper = DateAddExpr(date_add=NowExpr(now="timestamp"), unit="day", amount=-1)
    where = StructuredQuery.model_validate(
        {
            "from": "orders",
            "select": ["orders.id"],
            "where": {
                "and": [
                    {"col": "orders.created_at", "op": "gte", "value_expr": lower.model_dump()},
                    {"col": "orders.created_at", "op": "lte", "value_expr": upper.model_dump()},
                ]
            },
        }
    ).where
    from querygate.compiler.sqlalchemy_compiler import _compile_where

    condition = _compile_where(where, _tables(), {}, dialect)
    compiled = condition.compile(dialect=_DIALECTS[dialect])
    params = compiled.construct_params()
    # Two DISTINCT bind entries, not one shared/overwritten one.
    assert len(params) == 2, (
        f"expected 2 distinct bind params for the 2 date_add amounts, got {params!r} — "
        "a bare, non-unique bindparam name let the second date_add silently "
        "overwrite the first's bound value"
    )
    assert set(params.values()) == {-30, -1}, f"both amounts must survive intact, got {params!r}"


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


# BigQuery is the one dialect with a genuine gap here, found by integrating
# it into this shared suite (rather than the historical "no dialect has a
# genuine gap" claim below staying true by never being tested against it):
# `date_add(now(), unit, ...)` renders `now()` as a BigQuery TIMESTAMP (a
# timezone-independent absolute instant — see BigQueryDialectAdapter.
# current_timestamp), and BigQuery's TIMESTAMP_ADD genuinely lacks
# week/month/year (confirmed against Google's docs; see
# TestBigQueryDateAddTypeGaps in test_dialect_adapters.py for the dedicated,
# real coverage of this — including the DATETIME-typed operand case, where
# BigQuery has NO gap at all). Declared explicitly, the same pattern
# `_DECLARED_PART_GAPS` above uses, rather than silently excluding bigquery
# from the loop.
_DECLARED_UNIT_GAPS = {("bigquery", "week"), ("bigquery", "month"), ("bigquery", "year")}


@pytest.mark.parametrize("unit", list(IntervalUnit.__args__))
def test_every_interval_unit_renders_on_every_dialect(unit):
    """No dialect but BigQuery has a genuine gap here (see
    `_DECLARED_UNIT_GAPS`) — every other dialect can shift `now()` by every
    unit, so any OTHER failure is a missing mapping, not a capability gap."""
    expr = DateAddExpr(date_add=NowExpr(now="timestamp"), unit=unit, amount=1)
    for dialect in _DIALECTS:
        declared = (dialect, unit) in _DECLARED_UNIT_GAPS
        try:
            assert _sql(expr, dialect)
            rejected = False
        except QueryValidationError:
            rejected = True
        assert rejected == declared, f"{dialect}/{unit}: " + (
            "declared as a gap but rendered" if declared else "rejected but not a declared gap"
        )


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


def test_no_date_part_map_is_dead_code():
    """Every per-dialect part map must be the one the adapter actually reads.

    This exists because a refactor silently left `MSSQLDialectAdapter` on an
    inline `.get(part, part)` passthrough while `_MSSQL_DATEPART_FIELDS` sat
    beside it unused — so the fail-open behavior was still live, the map was
    dead, and a mutation test that edited the map proved nothing. Every other
    tier passed. Perturbing each map and observing the rendered SQL change is
    the only check that couples the map to the code path.
    """
    from querygate.compiler import dialect_adapters as da

    cases = [
        ("postgresql", da._PG_EXTRACT_FIELDS, "year", "_PG_EXTRACT_FIELDS"),
        ("mssql", da._MSSQL_DATEPART_FIELDS, "week", "_MSSQL_DATEPART_FIELDS"),
        ("mysql", da._MYSQL_EXTRACT_FIELDS, "year", "_MYSQL_EXTRACT_FIELDS"),
        ("sqlite", da._SQLITE_STRFTIME_PARTS, "year", "_SQLITE_STRFTIME_PARTS"),
    ]
    for dialect, mapping, part, name in cases:
        expr = ExtractExpr(extract={"col": "orders.created_at"}, part=part)
        original = mapping[part]
        baseline = _sql(expr, dialect)
        mapping[part] = "qg_sentinel_value"
        try:
            mutated = _sql(expr, dialect)
        finally:
            mapping[part] = original
        assert mutated != baseline and "qg_sentinel_value" in mutated, (
            f"{name} is not read by the {dialect} adapter — it is dead code, and "
            "the real lookup is somewhere else"
        )

    # The fourth dispatch map. It holds argument POSITIONS, not keywords, so a
    # string sentinel is not usable — move `day` to a different `make_interval`
    # slot and assert the rendered call changes.
    positions = da._PG_MAKE_INTERVAL_POSITIONS
    shift = DateAddExpr(date_add=NowExpr(now="timestamp"), unit="day", amount=5)
    baseline = _sql(shift, "postgresql")
    original = positions["day"]
    positions["day"] = 0 if original != 0 else 1
    try:
        mutated = _sql(shift, "postgresql")
    finally:
        positions["day"] = original
    assert mutated != baseline, (
        "_PG_MAKE_INTERVAL_POSITIONS is not read by the Postgres adapter — it is "
        "dead code, and the real position lookup is somewhere else"
    )

    # The two `date_add` unit maps. `_MSSQL_DATEADD_UNITS` is an IDENTITY
    # mapping, so an inline copy of it renders byte-identical SQL — no rendering
    # assertion, and no live differential run, could ever tell the difference.
    # The only observable coupling is the rejection: remove a unit from the map
    # and the adapter must refuse it. If it still renders, the map is dead.
    for dialect, target, unit in (
        ("mssql", "_MSSQL_DATEADD_UNITS", "day"),
        ("mysql", "_MYSQL_DATEADD_UNITS", "day"),
        ("sqlite", "_SQLITE_DATEADD_UNITS", "hour"),
    ):
        adapter = da.get_dialect_adapter(dialect)
        mapping = getattr(da, target)
        is_set = isinstance(mapping, frozenset)
        removed = mapping - {unit} if is_set else {k: v for k, v in mapping.items() if k != unit}
        original = mapping
        setattr(da, target, removed)
        try:
            with pytest.raises(QueryValidationError, match="not supported"):
                adapter.date_add(sa.column("c"), unit, 1)
        finally:
            setattr(da, target, original)


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "mysql", "sqlite", "bigquery"])
def test_an_unknown_date_part_raises_a_typed_error_not_a_keyerror(dialect):
    """A future `DatePart` member added without teaching an adapter must surface
    as a clean 4xx naming the dialect, never a KeyError 500 and never a silent
    passthrough into SQL text."""
    from querygate.compiler.dialect_adapters import get_dialect_adapter

    adapter = get_dialect_adapter(dialect)
    with pytest.raises(QueryValidationError, match="not supported"):
        adapter.extract_part("nanocentury", sa.column("c"))


@pytest.mark.parametrize("dialect", ["postgresql", "mssql", "mysql", "sqlite", "bigquery"])
def test_an_unknown_interval_unit_raises_a_typed_error_on_every_dialect(dialect):
    """The `date_add` half of the same rule, which the first version of the
    exhaustiveness refactor left out — it guarded `extract_part` on all three
    dialects but `date_add` on Postgres only.

    SQLite is why this matters most: an unmapped unit reaching its
    `datetime(x, '+N units')` modifier returns **NULL** rather than erroring, so
    a future `IntervalUnit` would have become a silently empty column — the
    exact failure `weeks` already caused once."""
    from querygate.compiler.dialect_adapters import get_dialect_adapter

    adapter = get_dialect_adapter(dialect)
    # BigQuery's date_add needs a CONCRETELY TYPED operand before it can even
    # get to the unit lookup (see _bq_temporal_kind's docstring) — an untyped
    # `sa.column("c")` hits that gap first, with a different message, which
    # is exactly what test_untyped_operand_is_rejected_not_guessed in
    # test_dialect_adapters.py covers separately. Give BigQuery a
    # DATETIME-typed column here so this test actually exercises the
    # "unknown unit" rejection it is named for, the same one every other
    # dialect's plain `sa.column("c")` already reaches.
    col = sa.column("c", type_=sa.DateTime) if dialect == "bigquery" else sa.column("c")
    with pytest.raises(QueryValidationError, match="not supported"):
        adapter.date_add(col, "nanocentury", 3)


def test_date_add_amount_is_bounded_to_int32():
    """`max_interval_days` bounds calendar REACH; this bounds the NUMBER handed
    to the dialect, and only the latter tracks T-SQL's own limit.

    Measured on SQL Server 2022: `DATEADD(second, 2147483647, …)` succeeds and
    `…, 2147483648` raises "Arithmetic overflow error converting expression to
    data type int" — a driver error, not a typed rejection. The two bounds
    diverge once a deployment raises `max_interval_days` above 24,855, since a
    `second`-unit amount inside the day-cap can still exceed int32.
    """
    # BOTH boundaries on the accepted side — an off-by-one at `ge` would
    # otherwise slip through with only the upper bound pinned.
    for edge in (2_147_483_647, -2_147_483_648):
        DateAddExpr.model_validate(
            {"date_add": {"now": "timestamp"}, "unit": "second", "amount": edge}
        )
    for over in (2_147_483_648, -2_147_483_649):
        with pytest.raises(pydantic.ValidationError):
            DateAddExpr.model_validate(
                {"date_add": {"now": "timestamp"}, "unit": "second", "amount": over}
            )


def test_a_raised_interval_cap_cannot_reach_the_mssql_int32_overflow():
    """The scenario the bound exists for, end to end: an operator raises
    `max_interval_days` well past the default, and a `second`-unit shift that
    the day-cap would happily admit is still refused before it can become a
    live `DATEADD` overflow."""
    generous = Policy(max_interval_days=100_000)
    # The int32 bound is an AST bound, so it fires at model construction and is
    # policy-independent — that half is covered above. What is new here is the
    # interaction: under a cap generous enough that `max_interval_days` would
    # NOT refuse a 2-billion-second shift, the request still validates only
    # because the amount is inside int32.
    validate_policy(
        _query({"date_add": {"now": "timestamp"}, "unit": "second", "amount": -2_000_000_000}),
        generous,
        "demo",
    )


# --------------------------------------------------------------------------- #
# `_validate_date_operands` — the unit tier the rule shipped without.
#
# It previously had only two integration cases, so its fail-OPEN branches (a
# type with no Python mapping) and its allow-list edges (time, interval) were
# untested — exactly the branches that decide whether a guard is a guard.
# Patches the module-level `_load_table`, which is the seam CLAUDE.md documents
# for testing schema validation without a database.
# --------------------------------------------------------------------------- #
_PROBE_COLUMNS = {
    "ts": sa.DateTime(),
    "d": sa.Date(),
    "t": sa.Time(),
    "span": sa.Interval(),  # Postgres `interval` -> timedelta
    "n": sa.Integer(),
    "s": sa.String(50),
    "unknown": sa.types.NullType(),  # vendor/unmapped type: python_type raises
}


def _patched_load_table(monkeypatch):
    import querygate.validation.schema_validation as sv

    metadata = sa.MetaData()
    table = sa.Table("events", metadata, *(sa.Column(n, t) for n, t in _PROBE_COLUMNS.items()))

    async def _load(connection_id, table_name, table_connection):
        return table

    monkeypatch.setattr(sv, "_load_table", _load)
    return sv


@pytest.mark.parametrize(
    "column,allowed",
    [
        ("ts", True),
        ("d", True),
        ("t", True),
        # Allowed deliberately: Postgres's `interval` maps to timedelta and
        # `EXTRACT(hour FROM interval_col)` is real Postgres. Rejecting it would
        # refuse a capability the dialect has.
        ("span", True),
        # Unmapped/vendor types fail OPEN — reject what is known-wrong, never
        # what is merely unrecognized.
        ("unknown", True),
        ("n", False),
        ("s", False),
    ],
)
def test_date_operand_type_rule_per_column_type(monkeypatch, column, allowed):
    import asyncio

    sv = _patched_load_table(monkeypatch)
    query = StructuredQuery.model_validate(
        {
            "from": "events",
            "select": [
                {"expr": {"extract": {"col": f"events.{column}"}, "part": "hour"}, "as": "v"}
            ],
        }
    )
    if allowed:
        asyncio.run(sv.validate_schema(query, "demo"))
    else:
        with pytest.raises(QueryValidationError, match="date/time column"):
            asyncio.run(sv.validate_schema(query, "demo"))


@pytest.mark.parametrize("column,allowed", [("ts", True), ("n", False), ("s", False)])
def test_date_bucket_is_held_to_the_same_operand_rule(monkeypatch, column, allowed):
    """item 117 — `date_bucket` is the THIRD date primitive, and item 102's rule
    originally covered only the two new ones while the docs described the general
    property. Measured over an INTEGER column: Postgres errors, MSSQL returns
    1900-01-02, the internal SQLite path returns -4712-01-05. Three backends,
    three different wrong answers, none usable — which is why closing it is a bug
    fix, not a behavior change."""
    import asyncio

    sv = _patched_load_table(monkeypatch)
    query = StructuredQuery.model_validate(
        {
            "from": "events",
            "select": [{"col": f"events.{column}", "granularity": "day", "as": "bucket"}],
        }
    )
    if allowed:
        asyncio.run(sv.validate_schema(query, "demo"))
    else:
        with pytest.raises(QueryValidationError, match="date/time column"):
            asyncio.run(sv.validate_schema(query, "demo"))


def test_every_date_primitive_is_covered_by_the_operand_rule():
    """The guard that would have caught item 117 at the time item 102 shipped.

    Enumerates the AST shapes that apply a date function to a column and asserts
    each is reachable through the one shared operand walk. A fourth date
    primitive added later fails here until it is wired in — which is exactly the
    drift that let `date_bucket` read as covered while it was not."""
    from querygate.validation.schema_validation import _iter_date_operands

    shapes = {
        "extract": {"select": [{"expr": {"extract": {"col": "t.c"}, "part": "hour"}, "as": "v"}]},
        "date_add": {
            "select": [
                {"expr": {"date_add": {"col": "t.c"}, "unit": "day", "amount": 1}, "as": "v"}
            ]
        },
        "date_bucket": {"select": [{"col": "t.c", "granularity": "day", "as": "v"}]},
    }
    for name, body in shapes.items():
        query = StructuredQuery.model_validate({"from": "t", **body})
        refs = [ref for ref, _label in _iter_date_operands(query)]
        assert refs == ["t.c"], f"{name} is not reached by the shared operand walk"


def test_date_operand_rule_reaches_a_where_predicate_and_a_subquery(monkeypatch):
    """Two positions the integration cases never covered."""
    import asyncio

    sv = _patched_load_table(monkeypatch)
    in_where = StructuredQuery.model_validate(
        {
            "from": "events",
            "select": ["events.ts"],
            "where": {
                "col": "events.ts",
                "op": "gte",
                "value_expr": {"date_add": {"col": "events.n"}, "unit": "day", "amount": 1},
            },
        }
    )
    with pytest.raises(QueryValidationError, match="date/time column"):
        asyncio.run(sv.validate_schema(in_where, "demo"))

    in_subquery = StructuredQuery.model_validate(
        {
            "from": "events",
            "select": ["events.ts"],
            "where": {
                "col": "events.n",
                "op": "in",
                "value_subquery": {
                    "from": "events",
                    "select": [
                        {"expr": {"extract": {"col": "events.n"}, "part": "year"}, "as": "v"}
                    ],
                },
            },
        }
    )
    with pytest.raises(QueryValidationError, match="date/time column"):
        asyncio.run(sv.validate_schema(in_subquery, "demo"))


def test_only_a_string_operand_is_told_to_cast(monkeypatch):
    """The remedy is scoped, because casting an INTEGER reproduces the very
    divergence the rule closes (Postgres errors on `CAST(int AS TIMESTAMP)`;
    MSSQL yields a 1900-epoch datetime). A string that holds a timestamp is the
    one case where the cast genuinely works."""
    import asyncio

    sv = _patched_load_table(monkeypatch)

    def _message(column: str) -> str:
        query = StructuredQuery.model_validate(
            {
                "from": "events",
                "select": [
                    {"expr": {"extract": {"col": f"events.{column}"}, "part": "hour"}, "as": "v"}
                ],
            }
        )
        with pytest.raises(QueryValidationError) as excinfo:
            asyncio.run(sv.validate_schema(query, "demo"))
        return str(excinfo.value)

    assert "Cast it first" in _message("s")
    assert "Cast it first" not in _message("n")
