"""Isolates every place SQL rendering genuinely differs per database engine
behind one small interface, so the rest of the compiler never branches on a
dialect string directly (TODO.md item 57, compiler-scoped slice).

Adding a dialect means implementing `DialectAdapter` once and registering it
in `_ADAPTERS` — not hunting through `sqlalchemy_compiler.py` for every place
a dialect assumption might have leaked in. Dropping a dialect means deleting
its adapter and registry entry. Scoped deliberately to the compiler's own
variance points (date bucketing, ORDER BY nulls handling, statistical
aggregate function names, window frame grammar, set-operation
availability) — `connections/dialects.py`'s
session guardrails
and `execution/cost_estimation.py`'s Postgres-only EXPLAIN hook are separate
concerns with their own dialect handling, not folded in here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Literal, Optional

import sqlalchemy as sa
from sqlalchemy.dialects import mssql

from querygate.connections.models import DatabaseDialect
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import ColumnMask, ColumnMaskKind


class DialectAdapter(ABC):
    """One implementation per supported dialect. Every method here is a
    point where two dialects render meaningfully different SQL for the same
    StructuredQuery concept — nothing dialect-universal (count/sum/avg/min/
    max, coalesce/lower/upper/trim/concat) belongs on this interface.
    """

    @abstractmethod
    def date_bucket(self, col: Any, granularity: str) -> Any:
        """Truncate a datetime column to day/week/month/quarter/year."""

    @abstractmethod
    def order_by_terms(
        self,
        col_expr: Any,
        direction: Literal["asc", "desc"],
        nulls: Optional[Literal["first", "last"]],
    ) -> List[Any]:
        """One or more ORDER BY terms implementing `direction` (+ `nulls`
        placement if set) for a single column/expression. A dialect whose SQL
        has no NULLS FIRST/LAST equivalent rejects a set `nulls` with
        QueryValidationError rather than synthesizing placement structure the
        AST never asked for (see MSSQLDialectAdapter; TODO.md item 74).
        """

    @abstractmethod
    def stat_fn(self, name: Literal["stddev", "variance"]) -> Callable[..., Any]:
        """The callable to apply to a column for a statistical aggregate."""

    @abstractmethod
    def string_agg(self, col_expr: Any, delimiter: str) -> Any:
        """Concatenate col_expr's grouped values into one delimiter-
        separated string, e.g. STRING_AGG(Customer.Email, ', ')."""

    @abstractmethod
    def array_agg(self, col_expr: Any) -> Any:
        """Collect col_expr's grouped values into a real array,
        e.g. ARRAY_AGG(OrderItem.Sku)."""

    @abstractmethod
    def percentile_cont(self, col_expr: Any, fraction: float) -> Any:
        """Continuous-interpolation percentile of col_expr's grouped
        values, e.g. PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY
        Order.TotalAmount) for the median."""

    @abstractmethod
    def scalar_function(self, name: str, args: List[Any]) -> Any:
        """Render one item-100 `FunctionExpr` whose SQL genuinely differs per
        dialect. Only the divergent few route here — `coalesce`, `lower`,
        `upper`, `trim`, `concat`, `abs`, `floor`, `nullif` and `replace` are
        identical everywhere and stay in the compiler's universal table, per
        this interface's own "nothing dialect-universal belongs here" rule.

        Arity is already checked at the AST layer, so an implementation may
        index `args` positionally. A dialect with no genuine equivalent for a
        function raises `QueryValidationError` rather than emulating one.
        """

    @abstractmethod
    def window_frame(
        self, mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
    ) -> Dict[str, Any]:
        """The `over()` keyword arguments rendering one item-101 window frame.

        `start`/`end` arrive in SQLAlchemy's own frame encoding (None = unbounded,
        0 = current row, -n = n preceding, +n = n following), so an implementation
        can inspect them without importing the AST. Returns `{"rows": (start, end)}`
        or `{"range_": (start, end)}`.

        A dialect that genuinely lacks a frame form rejects it with
        `QueryValidationError` pointing at the form to use instead — SQLAlchemy
        compiles every frame identically for every dialect with no guard of its
        own, so a real gap is invisible until it hits a live server (the items
        75/82 trap).
        """

    @abstractmethod
    def set_operation(self, op: str, all_rows: bool, selects: List[Any]) -> Any:
        """Combine `selects` with one item-104 set operation, returning a
        SQLAlchemy `CompoundSelect`.

        The keywords themselves (`UNION`, `UNION ALL`, `INTERSECT`, `EXCEPT`) are
        spelled identically on every supported backend, so the ONLY thing that
        varies here is which combinations exist: `INTERSECT ALL` / `EXCEPT ALL`
        are Postgres-only. A dialect without them rejects rather than silently
        dropping the `all` flag — dropping it would return *fewer* rows than the
        caller asked for with no error anywhere, and SQLAlchemy renders the
        invalid `INTERSECT ALL` for every dialect with no guard of its own
        (verified: it is a live syntax error, not a compile error — the items
        75/82 "renders fine, breaks live" trap).
        """

    @abstractmethod
    def extract_part(self, part: str, expr: Any) -> Any:
        """Render one item-102 `ExtractExpr` as an INTEGER-valued field of a
        date/timestamp: `EXTRACT(hour FROM x)` on Postgres, `DATEPART(hour, x)`
        on MSSQL.

        Two parts have a defined meaning the caller can rely on across dialects,
        so an implementation must render THAT definition rather than pass the
        keyword through: `dayofweek` is 0=Sunday..6=Saturday, and `week` is the
        ISO-8601 week number. T-SQL's natural spellings of both differ (1-based
        and `SET DATEFIRST`-dependent; a non-ISO week count), which is a
        different value silently returned — not a syntax error something else
        would catch.
        """

    @abstractmethod
    def current_timestamp(self, kind: Literal["timestamp", "date"]) -> Any:
        """Render one item-102 `NowExpr`. Always UTC (2026-07-26 Decision Log):
        `now()` under the UTC-pinned Postgres session, `SYSUTCDATETIME()` on
        MSSQL, `datetime('now')` on SQLite. "date" is midnight UTC today.
        """

    @abstractmethod
    def date_add(self, expr: Any, unit: str, amount: int) -> Any:
        """Render one item-102 `DateAddExpr` — shift `expr` by `amount` (signed)
        `unit`s. Postgres multiplies a unit interval, MSSQL uses `DATEADD`,
        SQLite a `datetime()` modifier. `amount` is already bounded by
        `Policy.max_interval_days` at validation time.
        """

    @abstractmethod
    def column_mask(self, col_expr: Any, mask: ColumnMask) -> Any:
        """Transform col_expr so the database itself returns a masked value
        (TODO.md item 49). NULL/BUCKET render identically everywhere; HASH and
        LAST use each dialect's own function idiom. A dialect with no genuine
        equivalent for a kind raises QueryValidationError rather than emulating
        it (see SQLiteDialectAdapter's HASH)."""


def _direction_expr(col_expr: Any, direction: Literal["asc", "desc"]) -> Any:
    return col_expr.asc() if direction == "asc" else col_expr.desc()


def _frame_kwargs(
    mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
) -> Dict[str, Any]:
    """The plain `over()` frame kwargs — shared by every dialect that supports the
    full frame grammar, so only a genuine per-dialect gap needs its own method body."""
    return {"rows" if mode == "rows" else "range_": (start, end)}


# Per-dialect spellings of each `DatePart`. These are EXHAUSTIVE lookups rather
# than `.get(part, part)` passthroughs, and that matters: a passthrough means a
# future `DatePart` member renders without any adapter author having considered
# it — plausibly valid on one dialect and a syntax error on another, which is
# the "renders fine, breaks live" asymmetry these adapters exist to prevent.
# `_missing_part` turns a gap into a typed error instead.
_PG_EXTRACT_FIELDS: Dict[str, str] = {
    "year": "year",
    "quarter": "quarter",
    "month": "month",
    "week": "week",  # Postgres's `week` IS the ISO-8601 week
    "day": "day",
    "dayofweek": "dow",  # 0=Sunday..6=Saturday, the definition we publish
    "dayofyear": "doy",
    "hour": "hour",
    "minute": "minute",
    "second": "second",
}

_MSSQL_DATEPART_FIELDS: Dict[str, str] = {
    "year": "year",
    "quarter": "quarter",
    "month": "month",
    # `iso_week`, not `week`: T-SQL's plain `week` is a DATEFIRST-dependent
    # count that disagrees with Postgres's ISO week for the same date.
    "week": "iso_week",
    "day": "day",
    # `dayofweek` is absent deliberately — it needs arithmetic, not a keyword.
    "dayofyear": "dayofyear",
    "hour": "hour",
    "minute": "minute",
    "second": "second",
}


def _missing_part(part: str, dialect: str) -> QueryValidationError:
    """The typed error a date-part gap raises, so a future enum member surfaces
    as a clean 4xx naming the dialect rather than a KeyError 500."""
    return QueryValidationError(f"extract part {part!r} is not supported on {dialect}")


def _missing_unit(unit: str, dialect: str) -> QueryValidationError:
    """The `date_add` sibling of `_missing_part`. Both exist for the same reason
    and both must, or the exhaustiveness doctrine holds for half the surface —
    which is what the first version of this refactor actually shipped."""
    return QueryValidationError(f"interval unit {unit!r} is not supported on {dialect}")


# The `date_add` unit vocabulary per dialect, exhaustive for the same reason the
# part maps are. SQLite is the sharpest case: an unmapped unit reaching its
# `datetime(x, '+N units')` modifier yields **NULL**, not an error — the exact
# silent-empty-column failure `weeks` caused before it was mapped to days.
_MSSQL_DATEADD_UNITS: Dict[str, str] = {
    "year": "year",
    "month": "month",
    "week": "week",
    "day": "day",
    "hour": "hour",
    "minute": "minute",
    "second": "second",
}

# `week` is present but is rewritten to days by the adapter (SQLite has no
# `weeks` modifier); membership here is what makes the unit *known*.
_SQLITE_DATEADD_UNITS: frozenset = frozenset(
    {"year", "month", "week", "day", "hour", "minute", "second"}
)


# Which positional argument of Postgres's `make_interval(years, months, weeks,
# days, hours, mins, secs)` each unit fills.
#
# `make_interval` rather than the shorter `amount * interval '1 day'`: that form
# has no integer operator: Postgres defines only `interval * double precision`,
# so the bound amount would be resolved to a float and the statement would
# depend on that inference. `make_interval`'s parameters are typed integers in
# fixed positions, so the caller's number binds as exactly what it is.
_PG_MAKE_INTERVAL_POSITIONS: Dict[str, int] = {
    "year": 0,
    "month": 1,
    "week": 2,
    "day": 3,
    "hour": 4,
    "minute": 5,
    "second": 6,
}


# strftime codes per date part. `%w` is 0=Sunday..6=Saturday and `%j` is the
# day of year — the same definitions the other two adapters render. `week` is
# absent deliberately (see SQLiteDialectAdapter.extract_part).
_SQLITE_STRFTIME_PARTS: Dict[str, str] = {
    "year": "%Y",
    "month": "%m",
    "day": "%d",
    "dayofweek": "%w",
    "dayofyear": "%j",
    "hour": "%H",
    "minute": "%M",
    "second": "%S",
}


# The SQLAlchemy constructor for each (op, all) pair. Exhaustive rather than a
# `getattr(sa, ...)` lookup, for the reason the date-part maps are: a future
# `SetOpKind` member must be given a constructor deliberately, not resolved by
# name into whatever happens to exist.
_SET_OPERATIONS: Dict[str, Dict[bool, Callable[..., Any]]] = {
    "union": {False: sa.union, True: sa.union_all},
    "intersect": {False: sa.intersect, True: sa.intersect_all},
    "except": {False: sa.except_, True: sa.except_all},
}


def _compound(op: str, all_rows: bool, selects: List[Any]) -> Any:
    """The plain constructor lookup, shared by every dialect that supports the
    requested combination — so only a genuine per-dialect gap needs its own body."""
    variants = _SET_OPERATIONS.get(op)
    if variants is None:
        raise QueryValidationError(f"Unsupported set operation {op!r}")
    return variants[all_rows](*selects)


def _no_all_variant(op: str, dialect: str) -> QueryValidationError:
    """The typed rejection for `INTERSECT ALL` / `EXCEPT ALL` on a dialect that
    only has the distinct forms (item 74's reject-don't-emulate rule).

    It says plainly that there is NO equivalent construct, rather than naming one.
    An earlier version suggested "keep duplicates with a union of the two sides",
    which is wrong in a way that returns data: `A UNION ALL B` is neither the
    multiset intersection nor the multiset difference — it is a superset of both.
    Item 74's own message points at a genuinely equivalent construction (a CASE
    bucket plus a tie-break sort); when no such construction exists, the honest
    thing is to say so, because a hint that silently returns the wrong rows is
    worse than no hint.
    """
    return QueryValidationError(
        f"{op.upper()} ALL is not supported on {dialect}: only {op.upper()} "
        "(which removes duplicates) exists there, and no other construct preserves "
        "duplicate multiplicity across it. Drop `all` for the de-duplicated form, "
        "or compute the multiset result client-side from each side's rows."
    )


def _bucket_mask(col_expr: Any, mask: ColumnMask) -> Any:
    """floor(col / size) * size — round a numeric value down to a bucket.
    Dialect-universal via sa.func.floor, so every adapter shares it."""
    return sa.func.floor(col_expr / mask.bucket_size) * mask.bucket_size


class PostgresDialectAdapter(DialectAdapter):
    def date_bucket(self, col: Any, granularity: str) -> Any:
        # Postgres has native date_trunc, covering every granularity directly.
        return sa.func.date_trunc(granularity, col)

    def order_by_terms(
        self,
        col_expr: Any,
        direction: Literal["asc", "desc"],
        nulls: Optional[Literal["first", "last"]],
    ) -> List[Any]:
        expr = _direction_expr(col_expr, direction)
        if nulls is None:
            return [expr]
        return [expr.nulls_first() if nulls == "first" else expr.nulls_last()]

    def stat_fn(self, name: Literal["stddev", "variance"]) -> Callable[..., Any]:
        return {"stddev": sa.func.stddev, "variance": sa.func.variance}[name]

    def string_agg(self, col_expr: Any, delimiter: str) -> Any:
        return sa.func.string_agg(col_expr, delimiter)

    def array_agg(self, col_expr: Any) -> Any:
        return sa.func.array_agg(col_expr)

    def percentile_cont(self, col_expr: Any, fraction: float) -> Any:
        return sa.within_group(sa.func.percentile_cont(fraction), col_expr)

    def scalar_function(self, name: str, args: List[Any]) -> Any:
        if name == "ceil":
            return sa.func.ceil(args[0])
        if name == "length":
            return sa.func.length(args[0])
        if name == "substring":
            return sa.func.substring(*args)
        if name == "round":
            # Postgres has no round(double precision, integer) — only
            # round(numeric, integer) — so a two-argument round over a float
            # column errors at runtime unless the value is numeric. Casting is
            # the mechanical per-dialect rendering of the SAME operation the
            # caller expressed (the date_bucket category), not structure the
            # AST didn't ask for. One-argument round is fine on any type.
            if len(args) == 2:
                return sa.func.round(sa.cast(args[0], sa.Numeric), args[1])
            return sa.func.round(args[0])
        raise QueryValidationError(f"Unsupported scalar function {name!r} on Postgres")

    def window_frame(
        self, mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
    ) -> Dict[str, Any]:
        # Postgres supports the full frame grammar, including RANGE with numeric
        # offsets (11+).
        return _frame_kwargs(mode, start, end)

    def set_operation(self, op: str, all_rows: bool, selects: List[Any]) -> Any:
        # Postgres is the only supported backend with all six combinations —
        # UNION/INTERSECT/EXCEPT each with and without ALL.
        return _compound(op, all_rows, selects)

    def extract_part(self, part: str, expr: Any) -> Any:
        # Postgres's own numbering IS the contract for both special parts —
        # `dow` is 0=Sunday..6=Saturday and `week` is the ISO week — so the map
        # only renames them. Postgres 14+ returns `numeric` from EXTRACT; the
        # cast makes the declared "integer" contract true rather than
        # dialect-dependent (a `Decimal` here and an `int` on MSSQL for the same
        # AST is precisely the differential-suite mismatch items 75/82 warn of).
        field = _PG_EXTRACT_FIELDS.get(part)
        if field is None:
            raise _missing_part(part, "Postgres")
        # FLOOR before the cast, and it is load-bearing for exactly one part.
        # Postgres's EXTRACT returns `numeric` INCLUDING the fractional second,
        # and `numeric -> integer` ROUNDS HALF AWAY FROM ZERO — so a plain cast
        # turns 30.6 into 31 and 59.7 into **60**, a value no clock produces and
        # that neither MSSQL's DATEPART nor SQLite's strftime('%S') can return
        # (both truncate). Flooring restores the one-definition-per-part
        # contract. A no-op for every integral part.
        return sa.cast(sa.func.floor(sa.extract(field, expr)), sa.Integer)

    def current_timestamp(self, kind: Literal["timestamp", "date"]) -> Any:
        # `now()` is timestamptz, so it compares exactly against a timestamptz
        # column with no timezone conversion at all. The UTC session pin in
        # connections/dialects.py is what additionally makes it correct against
        # a NAIVE timestamp column (Postgres converts one to the other using the
        # session TimeZone).
        if kind == "date":
            return sa.cast(sa.func.now(), sa.Date)
        return sa.func.now()

    def date_add(self, expr: Any, unit: str, amount: int) -> Any:
        # The caller's number is a BOUND PARAMETER in a fixed argument position;
        # `unit` only chooses which position. No caller-derived content reaches
        # the statement text (the plan's §9 hard rule).
        args: List[Any] = [0] * 7
        position = _PG_MAKE_INTERVAL_POSITIONS.get(unit)
        if position is None:
            raise QueryValidationError(f"interval unit {unit!r} is not supported on Postgres")
        args[position] = sa.literal(amount)
        return expr + sa.func.make_interval(*args)

    def column_mask(self, col_expr: Any, mask: ColumnMask) -> Any:
        if mask.kind is ColumnMaskKind.NULL:
            return sa.null()
        if mask.kind is ColumnMaskKind.BUCKET:
            return _bucket_mask(col_expr, mask)
        if mask.kind is ColumnMaskKind.LAST:
            return sa.func.right(sa.cast(col_expr, sa.Text), mask.length)
        # HASH — deterministic md5 of the text form.
        return sa.func.md5(sa.cast(col_expr, sa.Text))


class MSSQLDialectAdapter(DialectAdapter):
    def date_bucket(self, col: Any, granularity: str) -> Any:
        # MSSQL has no DATE_TRUNC; use the DATEADD/DATEDIFF truncation idiom.
        part = sa.literal_column(granularity)
        zero = sa.literal_column("0")
        return sa.func.dateadd(part, sa.func.datediff(part, zero, col), zero)

    def order_by_terms(
        self,
        col_expr: Any,
        direction: Literal["asc", "desc"],
        nulls: Optional[Literal["first", "last"]],
    ) -> List[Any]:
        expr = _direction_expr(col_expr, direction)
        if nulls is None:
            return [expr]
        # T-SQL has NO "NULLS FIRST/LAST" syntax at all (unlike Postgres/
        # SQLite), and SQLAlchemy's mssql dialect will still silently *compile*
        # an emitted `.nulls_last()` into that literal clause — a runtime
        # syntax error against a real server. Per CLAUDE.md's "expose
        # primitives, don't spoon-feed the agent" philosophy this is a hard
        # rejection, decided the same way as array_agg/percentile_cont below
        # (Decision Log, docs/PRODUCT_GUIDE.md; TODO.md item 74). QueryGate
        # previously injected an extra leading 0/1 CASE sort bucket the AST
        # never asked for to fake the placement — exactly the "engine solves
        # the agent's composition problem" shortcut that section rules out. An
        # agent that wants null placement on MSSQL expresses it directly with
        # primitives QueryGate already exposes: a CaseSelectItem 0/1 "is null"
        # bucket plus a leading OrderBySpec on it — the same workaround a human
        # T-SQL author writes by hand.
        raise QueryValidationError(
            "nulls first/last ordering is not supported on MSSQL: T-SQL has no "
            "NULLS FIRST/LAST syntax. Order by a CASE 0/1 'is null' bucket first "
            "to place nulls explicitly."
        )

    def stat_fn(self, name: Literal["stddev", "variance"]) -> Callable[..., Any]:
        return {"stddev": sa.func.STDEV, "variance": sa.func.VAR}[name]

    def string_agg(self, col_expr: Any, delimiter: str) -> Any:
        # SQL Server 2017+'s STRING_AGG — no ORDER BY/DISTINCT support inside
        # the call, matching the AST-level bound (query_ast/models.py's
        # StringAggSelectItem docstring).
        return sa.func.STRING_AGG(col_expr, delimiter)

    def array_agg(self, col_expr: Any) -> Any:
        # Unlike string_agg, there is no MSSQL equivalent to raise a real
        # gap for: T-SQL has no array/collection type at all, so there is
        # nothing to render into — not a missing-function gap that some
        # other T-SQL idiom could stand in for. Per CLAUDE.md's engine
        # philosophy, this stays a hard rejection rather than an emulation
        # (e.g. faking an array with STRING_AGG-as-CSV or a JSON trick).
        raise QueryValidationError(
            "array_agg is not supported on MSSQL: T-SQL has no array/collection "
            "type to hold the result"
        )

    def percentile_cont(self, col_expr: Any, fraction: float) -> Any:
        # T-SQL's PERCENTILE_CONT exists only as an analytic (window)
        # function requiring an OVER(...) clause — there is no GROUP BY-
        # compatible aggregate form the way Postgres's percentile_cont is.
        # Verified directly: SQLAlchemy's within_group() happily compiles
        # identical SQL text against the mssql dialect (no dialect-level
        # guard of its own), which would only fail at runtime against a
        # real SQL Server — the same "renders fine, breaks live" trap
        # item 75 flagged for stddev/variance before stat_fn existed. Per
        # CLAUDE.md's engine philosophy, this stays a hard rejection
        # rather than silently emitting SQL that can't actually run in
        # the plain-aggregate shape the AST expresses.
        raise QueryValidationError(
            "percentile_cont is not supported on MSSQL as a GROUP BY aggregate: "
            "T-SQL's PERCENTILE_CONT only exists as an analytic/window function "
            "requiring an OVER(...) clause"
        )

    def scalar_function(self, name: str, args: List[Any]) -> Any:
        if name == "ceil":
            # T-SQL spells it CEILING; there is no CEIL. Same operation, its
            # own idiom — the mechanical-translation case, not a real gap.
            return sa.func.CEILING(args[0])
        if name == "length":
            # LEN, not LENGTH. Note T-SQL's LEN ignores trailing spaces where
            # Postgres's length() does not; a caller who needs the exact byte/
            # char semantics can TRIM explicitly rather than have us synthesize
            # a DATALENGTH-based emulation the AST never asked for.
            return sa.func.LEN(args[0])
        if name == "substring":
            # SQLAlchemy's mssql compiler renders the comma form
            # SUBSTRING(x, start, len); Postgres gets the SQL-standard
            # SUBSTRING(x FROM start FOR len). Both are correct for their
            # dialect — this is why the AST requires exactly 3 arguments (T-SQL
            # has no 2-argument form, so allowing one would render fine on
            # Postgres and break live on MSSQL).
            return sa.func.substring(*args)
        if name == "round":
            # T-SQL's ROUND requires the length argument; one-argument round is
            # rendered as ROUND(x, 0), the identical operation.
            return sa.func.ROUND(args[0], args[1] if len(args) == 2 else 0)
        raise QueryValidationError(f"Unsupported scalar function {name!r} on MSSQL")

    def window_frame(
        self, mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
    ) -> Dict[str, Any]:
        # T-SQL's RANGE accepts ONLY `UNBOUNDED PRECEDING/FOLLOWING` and
        # `CURRENT ROW` — a numeric RANGE offset (`RANGE 6 PRECEDING`) is a real
        # capability gap, not a spelling difference, so it is rejected rather than
        # silently rewritten to ROWS (which has genuinely different semantics with
        # ties). SQLAlchemy compiles `RANGE 6 PRECEDING` for the mssql dialect
        # without complaint, so nothing else would catch this before a live
        # failure. ROWS with offsets is fully supported here.
        if mode == "range" and (start not in (None, 0) or end not in (None, 0)):
            raise QueryValidationError(
                "a RANGE frame with a numeric offset is not supported on MSSQL: "
                "T-SQL's RANGE accepts only unbounded_preceding/current_row/"
                "unbounded_following. Use mode 'rows' for an N-row window."
            )
        return _frame_kwargs(mode, start, end)

    def set_operation(self, op: str, all_rows: bool, selects: List[Any]) -> Any:
        # T-SQL's INTERSECT/EXCEPT are distinct-only — there is no ALL form of
        # either to translate into. UNION ALL is fully supported.
        if all_rows and op != "union":
            raise _no_all_variant(op, "MSSQL")
        return _compound(op, all_rows, selects)

    def extract_part(self, part: str, expr: Any) -> Any:
        if part == "dayofweek":
            # T-SQL's DATEPART(weekday) is 1-based AND its origin moves with the
            # server's `SET DATEFIRST` (itself set by the connection's language),
            # so passing it through would return a value that depends on server
            # config — the same class of defect as the timezone pin this item
            # also fixes, and invisible to any rendering-only assertion.
            # `(DATEPART(weekday, x) + @@DATEFIRST - 1) % 7` is the standard
            # DATEFIRST-independent idiom and yields Postgres's 0=Sunday..6=Sat
            # for every DATEFIRST value. This is mechanical translation of a
            # defined primitive (the date_bucket category), not structure the
            # AST didn't ask for.
            weekday = sa.func.DATEPART(sa.literal_column("weekday"), expr)
            return (weekday + sa.literal_column("@@DATEFIRST") - 1) % 7
        field = _MSSQL_DATEPART_FIELDS.get(part)
        if field is None:
            raise _missing_part(part, "MSSQL")
        return sa.func.DATEPART(sa.literal_column(field), expr)

    def current_timestamp(self, kind: Literal["timestamp", "date"]) -> Any:
        # SYSUTCDATETIME(), not GETDATE()/SYSDATETIME(): those return the
        # server's local wall clock. T-SQL has no session time zone to pin, so
        # the UTC choice has to be made in the function itself.
        #
        # `mssql.DATE`, not the generic `sa.Date`: the generic type renders
        # `DATETIME` against an UNCONNECTED mssql dialect (verified), which would
        # not truncate the time at all — `now: "date"` would silently keep the
        # clock reading instead of meaning midnight. This is the same
        # unconnected-vs-connected rendering trap item 100's `CAST(x AS text)`
        # rationale fell into; naming the concrete type removes the dependency.
        if kind == "date":
            return sa.cast(sa.func.SYSUTCDATETIME(), mssql.DATE)
        return sa.func.SYSUTCDATETIME()

    def date_add(self, expr: Any, unit: str, amount: int) -> Any:
        # T-SQL has no interval type — DATEADD is the whole idiom, and it takes
        # the unit as a keyword. The keyword comes from an exhaustive map, never
        # caller text; only `amount` is caller-supplied, and it binds.
        keyword = _MSSQL_DATEADD_UNITS.get(unit)
        if keyword is None:
            raise _missing_unit(unit, "MSSQL")
        return sa.func.DATEADD(sa.literal_column(keyword), sa.literal(amount), expr)

    def column_mask(self, col_expr: Any, mask: ColumnMask) -> Any:
        if mask.kind is ColumnMaskKind.NULL:
            return sa.null()
        if mask.kind is ColumnMaskKind.BUCKET:
            return _bucket_mask(col_expr, mask)
        text = sa.cast(col_expr, sa.Unicode)
        if mask.kind is ColumnMaskKind.LAST:
            return sa.func.RIGHT(text, mask.length)
        # HASH — CONVERT the SHA2_256 HASHBYTES digest to a hex string (style 2).
        return sa.func.CONVERT(
            sa.literal_column("VARCHAR(64)"),
            sa.func.HASHBYTES(sa.literal("SHA2_256"), text),
            sa.literal(2),
        )


class SQLiteDialectAdapter(DialectAdapter):
    """Internal test/example path only — SQLite is not a supported registry
    dialect (see `connections/models.py`'s `DatabaseDialect` docstring).
    """

    def date_bucket(self, col: Any, granularity: str) -> Any:
        if granularity == "day":
            return sa.func.date(sa.func.strftime("%Y-%m-%d", col))
        if granularity == "month":
            return sa.func.date(sa.func.strftime("%Y-%m-01", col))
        if granularity == "year":
            return sa.func.date(sa.func.strftime("%Y-01-01", col))
        if granularity == "week":
            return sa.func.date(col, "weekday 0", "-6 days")
        if granularity == "quarter":
            month = sa.cast(sa.func.strftime("%m", col), sa.Integer)
            quarter_start_month = ((month - 1) / 3) * 3 + 1
            return sa.func.date(
                sa.func.strftime("%Y", col)
                + "-"
                + sa.func.printf("%02d", quarter_start_month)
                + "-01"
            )
        raise QueryValidationError(f"Unsupported date_bucket granularity: {granularity!r}")

    def order_by_terms(
        self,
        col_expr: Any,
        direction: Literal["asc", "desc"],
        nulls: Optional[Literal["first", "last"]],
    ) -> List[Any]:
        # SQLite 3.30+ (2019) supports NULLS FIRST/LAST natively.
        expr = _direction_expr(col_expr, direction)
        if nulls is None:
            return [expr]
        return [expr.nulls_first() if nulls == "first" else expr.nulls_last()]

    def stat_fn(self, name: Literal["stddev", "variance"]) -> Callable[..., Any]:
        raise QueryValidationError(
            f"{name} is not supported on the internal SQLite test/example dialect"
        )

    def string_agg(self, col_expr: Any, delimiter: str) -> Any:
        # Unlike stat_fn above, SQLite's group_concat(expr, sep) is a real
        # equivalent with the identical 2-arg shape as Postgres's
        # string_agg/MSSQL's STRING_AGG — not a stub — so this genuinely
        # works rather than raising, which is what lets this feature get
        # real end-to-end execution test coverage instead of rendering-only
        # assertions. Concatenation order is implementation-defined here
        # (as it is on every dialect without ORDER BY-in-call support).
        return sa.func.group_concat(col_expr, delimiter)

    def array_agg(self, col_expr: Any) -> Any:
        # Unlike string_agg's group_concat, there is no genuine SQLite
        # equivalent here: json_group_array() returns a JSON-encoded
        # string, not a real array value — mapping it would be exactly the
        # forced-parity emulation CLAUDE.md's engine philosophy rules out,
        # not a lucky shape match. Raise the same way MSSQL does.
        raise QueryValidationError(
            "array_agg is not supported on the internal SQLite test/example dialect: "
            "json_group_array() returns a JSON string, not a real array"
        )

    def percentile_cont(self, col_expr: Any, fraction: float) -> Any:
        # SQLite has no ordered-set aggregate support at all (no
        # PERCENTILE_CONT, no WITHIN GROUP) — a different reason from
        # MSSQL's window-function-only restriction, but the same outcome.
        raise QueryValidationError(
            "percentile_cont is not supported on the internal SQLite test/example "
            "dialect: SQLite has no ordered-set aggregate (WITHIN GROUP) support"
        )

    def scalar_function(self, name: str, args: List[Any]) -> Any:
        if name == "ceil":
            return sa.func.ceil(args[0])
        if name == "length":
            return sa.func.length(args[0])
        if name == "substring":
            # SQLite's substr(); `substring` is only an alias from 3.34, so the
            # portable spelling is used for the internal test/example path.
            return sa.func.substr(*args)
        if name == "round":
            return sa.func.round(*args)
        raise QueryValidationError(
            f"Unsupported scalar function {name!r} on the internal SQLite test/example dialect"
        )

    def window_frame(
        self, mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
    ) -> Dict[str, Any]:
        # SQLite has full window support since 3.25 and RANGE offsets since 3.28,
        # which is why the end-to-end suite can execute real frames on this path.
        return _frame_kwargs(mode, start, end)

    def set_operation(self, op: str, all_rows: bool, selects: List[Any]) -> Any:
        # Same gap as MSSQL, verified by execution rather than by reading the
        # grammar: `INTERSECT ALL` compiles here and then fails with
        # `sqlite3.OperationalError: near "ALL": syntax error`.
        if all_rows and op != "union":
            raise _no_all_variant(op, "the internal SQLite test/example dialect")
        return _compound(op, all_rows, selects)

    def extract_part(self, part: str, expr: Any) -> Any:
        if part == "week":
            # No ISO-week support: strftime's `%W` is a Monday-based count that
            # disagrees with the ISO week this primitive is defined as, and `%V`
            # only exists from SQLite 3.46 (2024), newer than the builds CPython
            # ships. Reject rather than return a number that silently differs
            # from what Postgres/MSSQL return for the same date — same posture
            # as array_agg/percentile_cont below. Group by week with
            # date_bucket(week) instead, which IS implemented here.
            raise QueryValidationError(
                "extract part 'week' is not supported on the internal SQLite "
                "test/example dialect: it has no ISO-8601 week function. Use a "
                "date_bucket select item with granularity 'week' to group by week."
            )
        if part == "quarter":
            # The outer cast is load-bearing, not cosmetic: SQLAlchemy's `/` is
            # TRUE division, so month 2 gives 1.333 and only truncation turns it
            # back into quarter 1. Months are 1-12, so truncation is floor here.
            month = sa.cast(sa.func.strftime("%m", expr), sa.Integer)
            return sa.cast((month + 2) / 3, sa.Integer)
        code = _SQLITE_STRFTIME_PARTS.get(part)
        if code is None:
            raise _missing_part(part, "the internal SQLite test/example dialect")
        return sa.cast(sa.func.strftime(code, expr), sa.Integer)

    def current_timestamp(self, kind: Literal["timestamp", "date"]) -> Any:
        # SQLite's 'now' is already UTC, so no conversion is needed to match the
        # other two dialects.
        return sa.func.date("now") if kind == "date" else sa.func.datetime("now")

    def date_add(self, expr: Any, unit: str, amount: int) -> Any:
        # SQLite's modifier vocabulary has NO `weeks` — `datetime(x, '-2 weeks')`
        # returns NULL rather than erroring, so this would have been a silently
        # empty column, not a failure. Expressed in days instead, which is the
        # identical shift (a week is exactly 7 days, unlike months/years).
        #
        # Exhaustive, for the same reason: a future `IntervalUnit` reaching the
        # f-string below would render `'+3 nanocenturys'`, which SQLite answers
        # with NULL rather than an error — the identical silent-empty-column
        # failure `weeks` already caused once.
        if unit not in _SQLITE_DATEADD_UNITS:
            raise _missing_unit(unit, "the internal SQLite test/example dialect")
        if unit == "week":
            unit, amount = "day", amount * 7
        # The modifier is a STRING ('-7 days'), which is why `amount` is
        # formatted into it — but it is passed as a BOUND PARAMETER via
        # sa.literal, not concatenated into SQL text, so no caller value reaches
        # the statement. The explicit `+` on a positive amount is required:
        # SQLite reads a bare number as a different modifier form.
        modifier = f"{amount:+d} {unit}s"
        return sa.func.datetime(expr, sa.literal(modifier))

    def column_mask(self, col_expr: Any, mask: ColumnMask) -> Any:
        if mask.kind is ColumnMaskKind.NULL:
            return sa.null()
        if mask.kind is ColumnMaskKind.BUCKET:
            return _bucket_mask(col_expr, mask)
        if mask.kind is ColumnMaskKind.LAST:
            # SQLite substr(x, -n) returns the last n characters.
            return sa.func.substr(sa.cast(col_expr, sa.Text), -mask.length)
        # SQLite has no built-in hash function (no md5/HASHBYTES), so — like
        # array_agg/percentile_cont above — reject rather than emulate.
        raise QueryValidationError(
            "column_mask kind 'hash' is not supported on the internal SQLite "
            "test/example dialect: SQLite has no built-in hash function"
        )


_MYSQL_EXTRACT_FIELDS: Dict[str, str] = {
    "year": "year",
    "quarter": "quarter",
    "month": "month",
    "day": "day",
    "hour": "hour",
    "minute": "minute",
    "second": "second",
}

# DATE_ADD's unit keyword per IntervalUnit — exhaustive for the same reason
# every other per-dialect unit map in this module is: a future unit reaching
# an f-string unguarded would silently render an invalid keyword rather than
# raising a typed error.
_MYSQL_DATEADD_UNITS: Dict[str, str] = {
    "year": "YEAR",
    "month": "MONTH",
    "week": "WEEK",
    "day": "DAY",
    "hour": "HOUR",
    "minute": "MINUTE",
    "second": "SECOND",
}


class MySQLDialectAdapter(DialectAdapter):
    def date_bucket(self, col: Any, granularity: str) -> Any:
        # MySQL has no DATE_TRUNC; each granularity gets its own idiom, all
        # wrapped in DATE(...) so every branch zeroes the time-of-day the same
        # way Postgres's date_trunc does — DATE_SUB alone leaves the original
        # clock reading in place, which would silently disagree with Postgres/
        # MSSQL for the exact same query (verified live: without the outer
        # DATE(), 'week' returned a timestamp with the original time-of-day
        # still attached).
        if granularity == "day":
            return sa.func.date(col)
        if granularity == "week":
            # WEEKDAY() is 0=Monday..6=Sunday, so this steps back to the
            # Monday of the current week — the same ISO-week start Postgres's
            # date_trunc('week', ...) uses. SUBDATE(date, days) — the
            # 2-argument plain-integer form, not SUBDATE(date, INTERVAL ...)
            # — is documented as exactly DATE_SUB(date, INTERVAL days DAY);
            # using it (rather than an INTERVAL clause) lets the day count
            # stay a normal composable expression (WEEKDAY(col)) instead of
            # raw SQL text, since MySQL's INTERVAL clause has no bind-
            # parameter or sub-expression form of its own for the count.
            return sa.func.date(sa.func.subdate(col, sa.func.weekday(col)))
        if granularity == "month":
            return sa.func.str_to_date(sa.func.date_format(col, "%Y-%m-01"), "%Y-%m-%d")
        if granularity == "quarter":
            return sa.func.str_to_date(
                sa.func.concat(
                    sa.func.year(col),
                    "-",
                    sa.func.lpad((sa.func.quarter(col) - 1) * 3 + 1, 2, "0"),
                    "-01",
                ),
                "%Y-%m-%d",
            )
        if granularity == "year":
            return sa.func.str_to_date(sa.func.date_format(col, "%Y-01-01"), "%Y-%m-%d")
        raise QueryValidationError(f"Unsupported date_bucket granularity: {granularity!r}")

    def order_by_terms(
        self,
        col_expr: Any,
        direction: Literal["asc", "desc"],
        nulls: Optional[Literal["first", "last"]],
    ) -> List[Any]:
        expr = _direction_expr(col_expr, direction)
        if nulls is None:
            return [expr]
        # MySQL has NO "NULLS FIRST/LAST" syntax at all (unlike Postgres/
        # SQLite) — the same genuine gap as MSSQL, not a spelling difference,
        # so this is a hard rejection rather than synthesizing the CASE-bucket
        # workaround on the caller's behalf (item 74's reject-don't-emulate
        # posture; see MSSQLDialectAdapter above for the identical reasoning).
        raise QueryValidationError(
            "nulls first/last ordering is not supported on MySQL: MySQL has no "
            "NULLS FIRST/LAST syntax. Order by a CASE 0/1 'is null' bucket first "
            "to place nulls explicitly."
        )

    def stat_fn(self, name: Literal["stddev", "variance"]) -> Callable[..., Any]:
        # MySQL's bare STDDEV()/VARIANCE() are the POPULATION statistic
        # (equivalent to STDDEV_POP/VAR_POP) — a genuinely different number
        # from Postgres's bare stddev()/variance() (sample statistic, N-1
        # denominator) and from MSSQL's STDEV()/VAR() (also sample). Using the
        # bare names here would render fine and silently return a different
        # value than the identical query on Postgres/MSSQL — exactly the
        # "renders fine, breaks live" trap this module's date-part maps guard
        # against, just for a number instead of a date. STDDEV_SAMP/VAR_SAMP
        # are MySQL's real sample-statistic functions, matching the other two
        # dialects' semantics exactly.
        return {"stddev": sa.func.stddev_samp, "variance": sa.func.var_samp}[name]

    def string_agg(self, col_expr: Any, delimiter: str) -> Any:
        # MySQL's GROUP_CONCAT uses SEPARATOR as a keyword *inside* the call's
        # parens (`GROUP_CONCAT(col SEPARATOR 'sep')`), not a comma-separated
        # second argument — applying .op("SEPARATOR") to col_expr itself
        # (before it becomes group_concat's argument) renders exactly that,
        # verified against a live server.
        return sa.func.group_concat(col_expr.op("SEPARATOR")(delimiter))

    def array_agg(self, col_expr: Any) -> Any:
        # MySQL has JSON_ARRAYAGG(), but — like SQLite's json_group_array() —
        # it returns a JSON-encoded string, not a real array/collection type.
        # Mapping it would be the exact forced-parity emulation CLAUDE.md's
        # engine philosophy rules out, not a lucky shape match, so this stays
        # a hard rejection the same way SQLite's does.
        raise QueryValidationError(
            "array_agg is not supported on MySQL: JSON_ARRAYAGG() returns a JSON "
            "string, not a real array/collection type"
        )

    def percentile_cont(self, col_expr: Any, fraction: float) -> Any:
        # MySQL has no ordered-set aggregate support at all (no
        # PERCENTILE_CONT, no WITHIN GROUP) — the same gap as SQLite, for the
        # same reason.
        raise QueryValidationError(
            "percentile_cont is not supported on MySQL: MySQL has no ordered-set "
            "aggregate (WITHIN GROUP) support"
        )

    def scalar_function(self, name: str, args: List[Any]) -> Any:
        if name == "ceil":
            return sa.func.ceil(args[0])
        if name == "length":
            # MySQL's LENGTH() returns the BYTE length (multi-byte characters
            # count as more than one), unlike Postgres's character-counting
            # length() — a genuine encoding-dependent difference, not a
            # rendering choice; CHAR_LENGTH() would be the character-counting
            # form if a caller needs that instead, but this AST primitive maps
            # to the dialect's own "length" idiom the same way Postgres/MSSQL
            # already do.
            return sa.func.length(args[0])
        if name == "substring":
            return sa.func.substring(*args)
        if name == "round":
            # MySQL's ROUND(x) and ROUND(x, d) both work natively for any
            # numeric type — no numeric-cast trap like Postgres's
            # round(double precision, integer) gap.
            return sa.func.round(args[0], args[1]) if len(args) == 2 else sa.func.round(args[0])
        raise QueryValidationError(f"Unsupported scalar function {name!r} on MySQL")

    def window_frame(
        self, mode: Literal["rows", "range"], start: Optional[int], end: Optional[int]
    ) -> Dict[str, Any]:
        # MySQL 8.0+ supports the full window-frame grammar, including RANGE
        # with a numeric offset — verified against a live server, unlike
        # MSSQL's genuine RANGE-offset gap.
        return _frame_kwargs(mode, start, end)

    def set_operation(self, op: str, all_rows: bool, selects: List[Any]) -> Any:
        # MySQL 8.0.31+ added native INTERSECT/EXCEPT, but — like MSSQL —
        # only the distinct forms; there is no ALL variant of either.
        # UNION ALL is fully supported.
        if all_rows and op != "union":
            raise _no_all_variant(op, "MySQL")
        return _compound(op, all_rows, selects)

    def extract_part(self, part: str, expr: Any) -> Any:
        if part == "dayofweek":
            # DAYOFWEEK() is 1=Sunday..7=Saturday; subtracting 1 gives
            # Postgres's 0=Sunday..6=Saturday numbering exactly, with no
            # server-config dependency (unlike MSSQL's DATEFIRST-relative
            # DATEPART(weekday, ...), MySQL's DAYOFWEEK() is fixed regardless
            # of session/server settings).
            return sa.func.dayofweek(expr) - 1
        if part == "week":
            # WEEK(x, 3) is MySQL's mode-3 form: ISO 8601 week numbering
            # (Monday-first, week 1 = the first week with 4+ days) — verified
            # live to agree with Postgres's EXTRACT(week FROM ...) and
            # MSSQL's DATEPART(iso_week, ...) for the same date.
            return sa.func.week(expr, 3)
        if part == "dayofyear":
            return sa.func.dayofyear(expr)
        field = _MYSQL_EXTRACT_FIELDS.get(part)
        if field is None:
            raise _missing_part(part, "MySQL")
        # MySQL supports the SQL-standard EXTRACT(unit FROM expr) directly for
        # these fields — verified live it returns a plain integer already, no
        # PostgresDialectAdapter-style flooring/casting needed.
        return sa.extract(field, expr)

    def current_timestamp(self, kind: Literal["timestamp", "date"]) -> Any:
        # UTC_TIMESTAMP(), not NOW()/SYSDATE(): those return the server's
        # local wall clock. Like MSSQL, MySQL has no session time zone to pin
        # the way Postgres does, so the UTC choice is made in the function
        # itself rather than relying on session state.
        if kind == "date":
            return sa.cast(sa.func.utc_timestamp(), sa.Date)
        return sa.func.utc_timestamp()

    def date_add(self, expr: Any, unit: str, amount: int) -> Any:
        # MySQL's DATE_ADD takes the unit as a keyword inside an INTERVAL
        # clause, not a function argument — there is no parameterized form
        # for the keyword (the same shape as MSSQL's DATEADD keyword arg).
        # The keyword comes from an exhaustive map, never caller text; only
        # `amount` is caller-supplied, and it binds as a real parameter via
        # bindparams — no caller-derived content reaches the statement text.
        keyword = _MYSQL_DATEADD_UNITS.get(unit)
        if keyword is None:
            raise _missing_unit(unit, "MySQL")
        interval = sa.text(f"INTERVAL :amt {keyword}").bindparams(
            amt=amount
        )  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text
        return sa.func.date_add(expr, interval)

    def column_mask(self, col_expr: Any, mask: ColumnMask) -> Any:
        if mask.kind is ColumnMaskKind.NULL:
            return sa.null()
        if mask.kind is ColumnMaskKind.BUCKET:
            return _bucket_mask(col_expr, mask)
        if mask.kind is ColumnMaskKind.LAST:
            return sa.func.right(sa.cast(col_expr, sa.Text), mask.length)
        # HASH — SHA2-256 hex digest of the text form.
        return sa.func.sha2(sa.cast(col_expr, sa.Text), 256)


_ADAPTERS: Dict[str, DialectAdapter] = {
    DatabaseDialect.POSTGRESQL: PostgresDialectAdapter(),
    DatabaseDialect.MSSQL: MSSQLDialectAdapter(),
    DatabaseDialect.MYSQL: MySQLDialectAdapter(),
}
_FALLBACK = SQLiteDialectAdapter()


def get_dialect_adapter(dialect: str) -> DialectAdapter:
    """`dialect` is the live SQLAlchemy engine's own `dialect.name` (see
    `compile_structured_query`'s own docstring note on this) — a wider
    domain than the registry's supported dialects, legitimately "sqlite"
    for the internal-only test/example path. Anything not in `_ADAPTERS`
    falls back to the SQLite adapter, matching the pre-item-73 behavior of
    `_date_bucket_expr`.
    """
    return _ADAPTERS.get(dialect, _FALLBACK)
