"""Isolates every place SQL rendering genuinely differs per database engine
behind one small interface, so the rest of the compiler never branches on a
dialect string directly (TODO.md item 57, compiler-scoped slice).

Adding a dialect means implementing `DialectAdapter` once and registering it
in `_ADAPTERS` — not hunting through `sqlalchemy_compiler.py` for every place
a dialect assumption might have leaked in. Dropping a dialect means deleting
its adapter and registry entry. Scoped deliberately to the compiler's own
variance points (date bucketing, ORDER BY nulls handling, statistical
aggregate function names) — `connections/dialects.py`'s session guardrails
and `execution/cost_estimation.py`'s Postgres-only EXPLAIN hook are separate
concerns with their own dialect handling, not folded in here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Literal, Optional

import sqlalchemy as sa

from querygate.connections.models import DatabaseDialect
from querygate.core.exceptions import QueryValidationError


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
        placement if set) for a single column/expression.
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


def _direction_expr(col_expr: Any, direction: Literal["asc", "desc"]) -> Any:
    return col_expr.asc() if direction == "asc" else col_expr.desc()


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
        # SQLite) — SQLAlchemy's mssql dialect will still silently *compile*
        # `.nulls_last()` into that literal clause, which is a runtime syntax
        # error against a real server. Emulate with a leading 0/1 CASE sort
        # bucket instead: nulls land in one bucket, non-nulls in the other,
        # sorted ascending, then the real column direction breaks ties.
        null_bucket = 0 if nulls == "first" else 1
        other_bucket = 1 - null_bucket
        bucket = sa.case((col_expr.is_(None), null_bucket), else_=other_bucket)
        return [bucket.asc(), expr]

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


_ADAPTERS: Dict[str, DialectAdapter] = {
    DatabaseDialect.POSTGRESQL: PostgresDialectAdapter(),
    DatabaseDialect.MSSQL: MSSQLDialectAdapter(),
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
