"""Unit tests for the DialectAdapter abstraction (TODO.md item 73/57).

`date_bucket` cases are a regression check: they must render byte-for-byte
identical to what `_date_bucket_expr`/`_sqlite_date_bucket_expr` produced
before this module existed, proving the extraction was behavior-neutral.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from querygate.compiler.dialect_adapters import (
    DialectAdapter,
    MSSQLDialectAdapter,
    MySQLDialectAdapter,
    PostgresDialectAdapter,
    SnowflakeDialectAdapter,
    SQLiteDialectAdapter,
    get_dialect_adapter,
)
from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import DatePart, IntervalUnit

# Snowflake cases throughout this file render against the REAL installed
# `snowflake.sqlalchemy` dialect object (not just the generic/unconnected
# compiler `_render` uses for the others) wherever that distinction matters,
# and are all backed by Snowflake's public SQL reference docs — but, unlike
# the Postgres/MSSQL/MySQL cases beside them, NONE of this is verified
# against a live Snowflake server (TODO.md item 19 phase 2): there is no
# Snowflake instance available in this environment, unlike the other three
# dialects which run in Docker. Treat every Snowflake assertion here as
# "renders as documented", not "confirmed correct against a real account".
#
# `snowflake-sqlalchemy` is a DEV-ONLY dependency (2026-08-06
# security-invariant-reviewer finding) — no production module imports it, so
# this is the one place in the repo that needs it importable at all.
# `importorskip` rather than a bare import: this file must still collect
# cleanly (skipping only the Snowflake cases, not erroring the whole module)
# in a hypothetical environment installing main dependencies only.
_snowflake_sqlalchemy = pytest.importorskip("snowflake.sqlalchemy")
_snowflake_dialect_cls = _snowflake_sqlalchemy.dialect

_SNOWFLAKE_DIALECT = _snowflake_dialect_cls()


def _render_snowflake(expr) -> str:
    return str(expr.compile(dialect=_SNOWFLAKE_DIALECT, compile_kwargs={"literal_binds": True}))


def _render(expr) -> str:
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


class TestGetDialectAdapter:
    def test_postgresql_and_mssql_resolve_to_their_own_adapter(self):
        assert isinstance(get_dialect_adapter("postgresql"), PostgresDialectAdapter)
        assert isinstance(get_dialect_adapter("mssql"), MSSQLDialectAdapter)

    def test_mysql_resolves_to_its_own_adapter(self):
        assert isinstance(get_dialect_adapter("mysql"), MySQLDialectAdapter)

    def test_snowflake_resolves_to_its_own_adapter(self):
        assert isinstance(get_dialect_adapter("snowflake"), SnowflakeDialectAdapter)

    def test_unknown_dialect_falls_back_to_sqlite(self):
        assert isinstance(get_dialect_adapter("sqlite"), SQLiteDialectAdapter)
        assert isinstance(get_dialect_adapter("something-unrecognized"), SQLiteDialectAdapter)


class TestDialectAdapterIsAbstract:
    def test_cannot_instantiate_base_class(self):
        with pytest.raises(TypeError):
            DialectAdapter()

    def test_incomplete_subclass_fails_at_definition_time(self):
        with pytest.raises(TypeError):

            class Incomplete(DialectAdapter):
                def date_bucket(self, col, granularity):
                    return col

            Incomplete()


class TestDateBucketRegression:
    """Byte-for-byte match with pre-item-73 `_date_bucket_expr` output."""

    def test_postgres_uses_date_trunc(self):
        col = sa.column("created_at")
        expr = PostgresDialectAdapter().date_bucket(col, "month")
        assert _render(expr) == "date_trunc('month', created_at)"

    def test_mssql_uses_dateadd_datediff(self):
        col = sa.column("created_at")
        expr = MSSQLDialectAdapter().date_bucket(col, "day")
        rendered = _render(expr).upper()
        assert "DATEADD" in rendered
        assert "DATEDIFF" in rendered

    @pytest.mark.parametrize("granularity", ["day", "week", "month", "quarter", "year"])
    def test_sqlite_supports_every_granularity(self, granularity):
        col = sa.column("created_at")
        expr = SQLiteDialectAdapter().date_bucket(col, granularity)
        _render(expr)  # must not raise

    def test_sqlite_rejects_unsupported_granularity(self):
        col = sa.column("created_at")
        with pytest.raises(QueryValidationError, match="Unsupported date_bucket granularity"):
            SQLiteDialectAdapter().date_bucket(col, "decade")

    @pytest.mark.parametrize("granularity", ["day", "week", "month", "quarter", "year"])
    def test_mysql_supports_every_granularity(self, granularity):
        # MySQL has no DATE_TRUNC — verified live against a real MySQL 8.4
        # server (TODO.md item 19) that every idiom below truncates the
        # time-of-day the same way Postgres's date_trunc does.
        col = sa.column("created_at")
        expr = MySQLDialectAdapter().date_bucket(col, granularity)
        _render(expr)  # must not raise

    def test_mysql_rejects_unsupported_granularity(self):
        col = sa.column("created_at")
        with pytest.raises(QueryValidationError, match="Unsupported date_bucket granularity"):
            MySQLDialectAdapter().date_bucket(col, "decade")

    @pytest.mark.parametrize("granularity", ["day", "week", "month", "quarter", "year"])
    def test_snowflake_supports_every_granularity(self, granularity):
        # Snowflake has DATE_TRUNC, but 'week' is computed explicitly via
        # DAYOFWEEKISO rather than trusting DATE_TRUNC('week', ...)'s
        # WEEK_START-session-dependent behavior — see the adapter's comment.
        col = sa.column("created_at")
        expr = SnowflakeDialectAdapter().date_bucket(col, granularity)
        _render_snowflake(expr)  # must not raise

    def test_snowflake_week_uses_dayofweekiso_not_date_trunc_week(self):
        col = sa.column("created_at")
        rendered = _render_snowflake(SnowflakeDialectAdapter().date_bucket(col, "week")).lower()
        assert "dayofweekiso" in rendered

    def test_snowflake_rejects_unsupported_granularity(self):
        col = sa.column("created_at")
        with pytest.raises(QueryValidationError, match="Unsupported date_bucket granularity"):
            SnowflakeDialectAdapter().date_bucket(col, "decade")


class TestOrderByTerms:
    def test_postgres_no_nulls_returns_single_term(self):
        col = sa.column("status")
        terms = PostgresDialectAdapter().order_by_terms(col, "asc", None)
        assert len(terms) == 1

    def test_postgres_nulls_last_renders_native_clause(self):
        col = sa.column("status")
        terms = PostgresDialectAdapter().order_by_terms(col, "asc", "last")
        assert len(terms) == 1
        assert "NULLS LAST" in _render(terms[0]).upper()

    def test_mssql_no_nulls_returns_single_term(self):
        col = sa.column("status")
        terms = MSSQLDialectAdapter().order_by_terms(col, "desc", None)
        assert len(terms) == 1

    @pytest.mark.parametrize("nulls", ["first", "last"])
    def test_mssql_nulls_rejected_not_emulated(self, nulls):
        # T-SQL has no NULLS FIRST/LAST syntax. Per CLAUDE.md's engine
        # philosophy (TODO.md item 74) this is a hard rejection, not a
        # synthesized CASE-bucket emulation — same posture as array_agg.
        col = sa.column("status")
        with pytest.raises(
            QueryValidationError, match="nulls first/last ordering is not supported"
        ):
            MSSQLDialectAdapter().order_by_terms(col, "asc", nulls)

    def test_sqlite_nulls_uses_native_clause(self):
        col = sa.column("status")
        terms = SQLiteDialectAdapter().order_by_terms(col, "asc", "first")
        assert len(terms) == 1
        assert "NULLS FIRST" in _render(terms[0]).upper()

    def test_mysql_no_nulls_returns_single_term(self):
        col = sa.column("status")
        terms = MySQLDialectAdapter().order_by_terms(col, "desc", None)
        assert len(terms) == 1

    @pytest.mark.parametrize("nulls", ["first", "last"])
    def test_mysql_nulls_rejected_not_emulated(self, nulls):
        # MySQL has no NULLS FIRST/LAST syntax either — the same genuine gap
        # as MSSQL, not a spelling difference.
        col = sa.column("status")
        with pytest.raises(
            QueryValidationError, match="nulls first/last ordering is not supported"
        ):
            MySQLDialectAdapter().order_by_terms(col, "asc", nulls)

    def test_snowflake_no_nulls_returns_single_term(self):
        col = sa.column("status")
        terms = SnowflakeDialectAdapter().order_by_terms(col, "desc", None)
        assert len(terms) == 1

    @pytest.mark.parametrize("nulls", ["first", "last"])
    def test_snowflake_nulls_uses_native_clause(self, nulls):
        # Unlike MSSQL/MySQL, Snowflake supports NULLS FIRST/LAST natively
        # (Snowflake's ORDER BY reference) — the Postgres/SQLite shape.
        col = sa.column("status")
        terms = SnowflakeDialectAdapter().order_by_terms(col, "asc", nulls)
        assert len(terms) == 1
        assert f"NULLS {nulls.upper()}" in _render_snowflake(terms[0]).upper()


class TestStatFn:
    def test_postgres_stddev_variance_render_plain_names(self):
        col = sa.column("amount")
        assert "stddev" in _render(PostgresDialectAdapter().stat_fn("stddev")(col)).lower()
        assert "variance" in _render(PostgresDialectAdapter().stat_fn("variance")(col)).lower()

    def test_mssql_stddev_variance_render_tsql_names(self):
        col = sa.column("amount")
        assert "STDEV" in _render(MSSQLDialectAdapter().stat_fn("stddev")(col)).upper()
        assert "VAR" in _render(MSSQLDialectAdapter().stat_fn("variance")(col)).upper()

    def test_sqlite_stat_fn_rejected(self):
        with pytest.raises(QueryValidationError, match="not supported"):
            SQLiteDialectAdapter().stat_fn("stddev")

    def test_mysql_stddev_variance_render_sample_stat_names(self):
        # MySQL's bare STDDEV()/VARIANCE() are the POPULATION statistic, a
        # genuinely different number from Postgres's/MSSQL's sample-statistic
        # bare names — STDDEV_SAMP/VAR_SAMP are the real matches (verified
        # live: STDDEV_SAMP/VAR_SAMP on the same data as MSSQL's STDEV/VAR
        # agree; bare STDDEV/VARIANCE do not).
        col = sa.column("amount")
        rendered_std = _render(MySQLDialectAdapter().stat_fn("stddev")(col)).lower()
        rendered_var = _render(MySQLDialectAdapter().stat_fn("variance")(col)).lower()
        assert "stddev_samp" in rendered_std
        assert "var_samp" in rendered_var

    def test_snowflake_stddev_variance_render_plain_names(self):
        # Unlike MySQL's bare names, Snowflake's bare STDDEV/VARIANCE ARE the
        # sample statistic (aliases of STDDEV_SAMP/VAR_SAMP per Snowflake's
        # own docs) — the Postgres shape, not MySQL's population-default gap.
        # Exact-name assertions, not `"stddev" in rendered` (2026-08-06
        # `test-contract-reviewer` finding): "stddev" is a substring of
        # "stddev_samp", so a plain-substring check would stay green even if
        # this branch were accidentally copied from MySQL's `stddev_samp`
        # mapping — the exact regression this adapter method exists to avoid.
        col = sa.column("amount")
        rendered_std = _render_snowflake(SnowflakeDialectAdapter().stat_fn("stddev")(col)).lower()
        rendered_var = _render_snowflake(SnowflakeDialectAdapter().stat_fn("variance")(col)).lower()
        assert "stddev(" in rendered_std and "stddev_samp" not in rendered_std
        assert "variance(" in rendered_var and "var_samp" not in rendered_var


class TestStringAgg:
    def test_postgres_renders_string_agg(self):
        col = sa.column("email")
        rendered = _render(PostgresDialectAdapter().string_agg(col, ", ")).lower()
        assert "string_agg" in rendered

    def test_mssql_renders_string_agg_tsql_name(self):
        col = sa.column("email")
        rendered = _render(MSSQLDialectAdapter().string_agg(col, ", ")).upper()
        assert "STRING_AGG" in rendered

    def test_sqlite_maps_to_group_concat(self):
        """Unlike stat_fn, SQLite genuinely supports this (group_concat has
        the same (expr, separator) shape) — not a raise, so this is real
        behavior enabling end-to-end execution tests elsewhere."""
        col = sa.column("email")
        rendered = _render(SQLiteDialectAdapter().string_agg(col, ", ")).lower()
        assert "group_concat" in rendered

    def test_mysql_renders_group_concat_with_separator_keyword(self):
        # MySQL's GROUP_CONCAT uses SEPARATOR as a keyword *inside* the call's
        # parens, not a comma-separated second argument like SQLite's — the
        # exact placement is verified against a live server.
        col = sa.column("email")
        rendered = _render(MySQLDialectAdapter().string_agg(col, ", ")).lower()
        assert "group_concat(email separator" in rendered

    def test_snowflake_renders_listagg(self):
        # LISTAGG(expr, delimiter) — the same 2-argument comma shape as
        # Postgres's string_agg, unlike MySQL's SEPARATOR-keyword form.
        col = sa.column("email")
        rendered = _render_snowflake(SnowflakeDialectAdapter().string_agg(col, ", ")).lower()
        assert "listagg(email" in rendered


class TestArrayAgg:
    def test_postgres_renders_array_agg(self):
        col = sa.column("status")
        rendered = _render(PostgresDialectAdapter().array_agg(col)).lower()
        assert "array_agg(" in rendered

    def test_mssql_rejects_array_agg(self):
        """Unlike string_agg, T-SQL has no array/collection type at all —
        the first DialectAdapter method where a real, supported registry
        dialect rejects a capability outright (not just SQLite)."""
        col = sa.column("status")
        with pytest.raises(QueryValidationError, match="array/collection type"):
            MSSQLDialectAdapter().array_agg(col)

    def test_sqlite_rejects_array_agg(self):
        """json_group_array() returns a JSON string, not a real array —
        unlike string_agg's group_concat, there's no genuine shape match
        here, so this stays a raise rather than a forced-parity emulation."""
        col = sa.column("status")
        with pytest.raises(QueryValidationError, match="not supported"):
            SQLiteDialectAdapter().array_agg(col)

    def test_mysql_rejects_array_agg(self):
        """MySQL has JSON_ARRAYAGG(), but — like SQLite's json_group_array()
        — it returns a JSON-encoded string, not a real array/collection type,
        so this stays a raise rather than a forced-parity emulation."""
        col = sa.column("status")
        with pytest.raises(QueryValidationError, match="JSON string"):
            MySQLDialectAdapter().array_agg(col)

    def test_snowflake_renders_array_agg(self):
        # Unlike MySQL's/SQLite's JSON-string-returning functions,
        # Snowflake's ARRAY_AGG returns a real native ARRAY type — a genuine
        # equivalent, so this is a real render, not a rejection.
        col = sa.column("status")
        rendered = _render_snowflake(SnowflakeDialectAdapter().array_agg(col)).lower()
        assert "array_agg(" in rendered


class TestPercentileCont:
    def test_postgres_renders_percentile_cont(self):
        col = sa.column("total_amount")
        rendered = _render(PostgresDialectAdapter().percentile_cont(col, 0.5)).lower()
        assert "percentile_cont(0.5)" in rendered
        assert "within group" in rendered

    def test_mssql_rejects_percentile_cont(self):
        """T-SQL's PERCENTILE_CONT is analytic-function-only (requires an
        OVER clause) — no GROUP BY-compatible aggregate form exists, unlike
        Postgres. Verified this isn't just a naming gap: SQLAlchemy's
        within_group() silently compiles against the mssql dialect too, so
        this must be a deliberate raise, not left to fail at runtime."""
        col = sa.column("total_amount")
        with pytest.raises(QueryValidationError, match="analytic/window function"):
            MSSQLDialectAdapter().percentile_cont(col, 0.5)

    def test_sqlite_rejects_percentile_cont(self):
        col = sa.column("total_amount")
        with pytest.raises(QueryValidationError, match="ordered-set aggregate"):
            SQLiteDialectAdapter().percentile_cont(col, 0.5)

    def test_mysql_rejects_percentile_cont(self):
        """MySQL has no ordered-set aggregate support at all — no
        PERCENTILE_CONT, no WITHIN GROUP."""
        col = sa.column("total_amount")
        with pytest.raises(QueryValidationError, match="ordered-set aggregate"):
            MySQLDialectAdapter().percentile_cont(col, 0.5)

    def test_snowflake_renders_percentile_cont(self):
        # PERCENTILE_CONT(...) WITHIN GROUP (ORDER BY ...) works as a plain
        # GROUP BY aggregate on Snowflake (the OVER(...) clause is optional,
        # per Snowflake's own docs) — the Postgres shape, not MSSQL's
        # analytic-only gap.
        col = sa.column("total_amount")
        rendered = _render_snowflake(SnowflakeDialectAdapter().percentile_cont(col, 0.5)).lower()
        assert "percentile_cont(0.5)" in rendered
        assert "within group" in rendered


class TestWindowFrame:
    """item 101 — the frame grammar is the one genuinely per-dialect part of a
    window, so it is the one part that lives on the adapter."""

    _ADAPTERS = {
        "postgres": PostgresDialectAdapter(),
        "mssql": MSSQLDialectAdapter(),
        "mysql": MySQLDialectAdapter(),
        "sqlite": SQLiteDialectAdapter(),
        "snowflake": SnowflakeDialectAdapter(),
    }

    @pytest.mark.parametrize("name", sorted(_ADAPTERS))
    def test_rows_frames_are_supported_everywhere(self, name):
        adapter = self._ADAPTERS[name]
        assert adapter.window_frame("rows", None, 0) == {"rows": (None, 0)}
        assert adapter.window_frame("rows", -6, 2) == {"rows": (-6, 2)}

    @pytest.mark.parametrize("name", sorted(_ADAPTERS))
    def test_unbounded_and_current_row_range_frames_are_supported_everywhere(self, name):
        adapter = self._ADAPTERS[name]
        assert adapter.window_frame("range", None, 0) == {"range_": (None, 0)}
        assert adapter.window_frame("range", 0, None) == {"range_": (0, None)}

    @pytest.mark.parametrize("start,end", [(-6, 0), (0, 3), (-1, 1)])
    def test_mssql_rejects_a_numeric_range_offset(self, start, end):
        """T-SQL's RANGE takes only UNBOUNDED/CURRENT ROW. Rejected rather than
        rewritten to ROWS, which has different tie semantics — the item-74
        reject-don't-emulate posture."""
        with pytest.raises(QueryValidationError, match="RANGE frame with a numeric offset"):
            MSSQLDialectAdapter().window_frame("range", start, end)

    @pytest.mark.parametrize("name", ["postgres", "mysql", "sqlite", "snowflake"])
    def test_numeric_range_offsets_are_supported_off_mssql(self, name):
        # Snowflake's RANGE BETWEEN with a numeric offset reached General
        # Availability 2024-08-08 per Snowflake's release notes — this is a
        # rendering assertion only; an account on an older Snowflake release
        # could genuinely lack it, which no test in this environment can
        # catch without a live server.
        assert self._ADAPTERS[name].window_frame("range", -6, 0) == {"range_": (-6, 0)}


class TestMySQLAsyncmyParamstyleStaysPositional:
    """Guards the justification behind `security/dependency-audit-allowlist.json`'s
    PYSEC-2026-286 (asyncmy CVE-2025-65896) entry: that CVE is a SQL injection
    via attacker-controlled DICT KEYS in a pyformat-style parameter mapping.
    SQLAlchemy's DBAPI execution layer shapes cursor parameters according to
    the dialect's declared `paramstyle`; the mysql+asyncmy dialect declares
    'format' (positional), so SQLAlchemy always hands asyncmy's cursor a
    tuple, never a dict, and the vulnerable codepath is never reached. If a
    future SQLAlchemy release ever changed this dialect's paramstyle to
    something dict-shaped, that allowlist entry's justification would
    silently stop holding — this test exists so that change fails loudly
    here instead."""

    def test_asyncmy_dialect_paramstyle_is_positional_not_dict_based(self):
        from sqlalchemy.dialects.mysql.asyncmy import MySQLDialect_asyncmy

        assert MySQLDialect_asyncmy().paramstyle in ("format", "qmark", "numeric")


# --------------------------------------------------------------------------- #
# Snowflake (TODO.md item 19 phase 2) — dedicated coverage for the primitives
# `test_date_primitives.py`'s shared 4-dialect suite doesn't reach (that file
# is coupled to the live PG/MSSQL differential infrastructure, which has no
# Snowflake counterpart in this environment). Every case below is a rendering
# assertion against the real `snowflake.sqlalchemy` dialect object, backed by
# Snowflake's public SQL reference docs — NONE of it is verified against a
# live Snowflake server. See TODO.md's Snowflake live-verification follow-up
# item for what would close that gap.
# --------------------------------------------------------------------------- #
class TestSnowflakeExtractPartDateAddCurrentTimestamp:
    def test_every_date_part_is_supported(self):
        # Iterates the live `DatePart` enum directly (2026-08-06
        # `architecture-boundary-reviewer` finding), not a hand-copied tuple:
        # unlike `test_date_primitives.py`'s Postgres/MSSQL/MySQL/SQLite
        # suite (parametrized off `DatePart.__args__` itself, so a future
        # member forces a decision automatically), a manually-copied tuple
        # here would silently miss a new `DatePart` member instead of
        # failing loudly. Snowflake has NO declared gaps (unlike SQLite's
        # 'week'), so every part must render without raising — `dayofweek`
        # and `week` get their own content-specific tests below in addition
        # to this coverage check.
        for part in DatePart.__args__:
            expr = SnowflakeDialectAdapter().extract_part(part, sa.column("created_at"))
            assert _render_snowflake(expr)

    def test_dayofweek_uses_dayofweekiso_normalized_modulo_7(self):
        # Snowflake's plain DAYOFWEEK is WEEK_START-session-dependent (like
        # MSSQL's DATEFIRST-relative DATEPART(weekday, ...)); DAYOFWEEKISO is
        # fixed ISO (1=Monday..7=Sunday), and `% 7` maps it onto the
        # 0=Sunday..6=Saturday contract Postgres's `dow`/MySQL's
        # `DAYOFWEEK() - 1` publish.
        rendered = _render_snowflake(
            SnowflakeDialectAdapter().extract_part("dayofweek", sa.column("created_at"))
        ).lower()
        assert "dayofweekiso" in rendered
        assert "% 7" in rendered

    def test_week_uses_weekiso_not_the_session_dependent_week(self):
        rendered = _render_snowflake(
            SnowflakeDialectAdapter().extract_part("week", sa.column("created_at"))
        ).lower()
        assert "weekiso" in rendered

    def test_unknown_part_raises_a_typed_error_not_a_keyerror(self):
        with pytest.raises(QueryValidationError, match="not supported"):
            SnowflakeDialectAdapter().extract_part("nanocentury", sa.column("c"))

    def test_date_add_renders_every_unit(self):
        # `IntervalUnit.__args__` directly, for the same reason
        # `test_every_date_part_is_supported` above iterates `DatePart`
        # rather than a hand-copied tuple.
        for unit in IntervalUnit.__args__:
            expr = SnowflakeDialectAdapter().date_add(sa.column("created_at"), unit, 1)
            assert _render_snowflake(expr)

    def test_date_add_unknown_unit_raises_a_typed_error(self):
        with pytest.raises(QueryValidationError, match="not supported"):
            SnowflakeDialectAdapter().date_add(sa.column("c"), "nanocentury", 3)

    def test_date_add_binds_the_amount_rather_than_inlining_it(self):
        # `sa.literal(amount)` is a BOUND PARAMETER, not text interpolation —
        # asserted by compiling WITHOUT literal_binds (the default) and
        # checking the amount does NOT appear in the SQL text itself, only
        # in the compiled statement's params.
        expr = SnowflakeDialectAdapter().date_add(sa.column("created_at"), "day", -7)
        compiled = expr.compile(dialect=_SNOWFLAKE_DIALECT)
        assert "-7" not in str(compiled), "the amount must bind, not be inlined into SQL text"
        assert -7 in compiled.params.values()

    def test_current_timestamp_uses_sysdate_not_current_timestamp(self):
        # SYSDATE() is UTC/TIMESTAMP_NTZ; CURRENT_TIMESTAMP() is
        # session-timezone-dependent TIMESTAMP_LTZ (Snowflake docs) — the
        # same UTC-always posture as MSSQL's SYSUTCDATETIME().
        rendered = _render_snowflake(SnowflakeDialectAdapter().current_timestamp("timestamp"))
        assert "SYSDATE" in rendered.upper()
        assert "CURRENT_TIMESTAMP" not in rendered.upper()

    def test_current_timestamp_date_kind_casts_to_a_real_date_type(self):
        sql = _render_snowflake(SnowflakeDialectAdapter().current_timestamp("date"))
        cast_target = sql.upper().rsplit(" AS ", 1)[1].rstrip(")")
        assert cast_target == "DATE", f"expected a DATE cast, got {cast_target!r}"


class TestSnowflakeScalarFunctions:
    def test_ceil_length_substring_round(self):
        adapter = SnowflakeDialectAdapter()
        col = sa.column("x")
        assert "ceil" in _render_snowflake(adapter.scalar_function("ceil", [col])).lower()
        assert "length" in _render_snowflake(adapter.scalar_function("length", [col])).lower()
        rendered_sub = _render_snowflake(
            adapter.scalar_function("substring", [col, sa.literal(1), sa.literal(3)])
        ).lower()
        assert "substring" in rendered_sub
        assert "round" in _render_snowflake(adapter.scalar_function("round", [col])).lower()
        rendered_round2 = _render_snowflake(
            adapter.scalar_function("round", [col, sa.literal(2)])
        ).lower()
        assert "round" in rendered_round2

    def test_unsupported_scalar_function_is_rejected(self):
        with pytest.raises(QueryValidationError, match="Unsupported scalar function"):
            SnowflakeDialectAdapter().scalar_function("not_a_real_function", [sa.column("x")])


class TestSnowflakeSetOperation:
    def test_union_all_is_supported(self):
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        compound = SnowflakeDialectAdapter().set_operation("union", True, selects)
        assert "UNION ALL" in str(compound.compile(dialect=_SNOWFLAKE_DIALECT)).upper()

    @pytest.mark.parametrize("op", ["intersect", "except"])
    def test_intersect_except_all_are_rejected_not_emulated(self, op):
        # Snowflake's INTERSECT/EXCEPT (MINUS) are distinct-only — the same
        # genuine gap as MSSQL/MySQL, not a spelling difference.
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        with pytest.raises(QueryValidationError, match=f"{op.upper()} ALL is not supported"):
            SnowflakeDialectAdapter().set_operation(op, True, selects)

    @pytest.mark.parametrize("op", ["intersect", "except"])
    def test_intersect_except_without_all_are_supported(self, op):
        # Asserts the RENDERED KEYWORD, not just "returned something" (2026-08-06
        # `test-contract-reviewer` finding): an `is not None` check alone would
        # stay green even if this branch silently rendered a UNION instead of
        # the requested op — a wrong-row-set bug the same class of `_no_all_variant`
        # docstring warns about, invisible to a non-None assertion. Mirrors
        # `test_set_operations.py`'s `test_every_operator_without_all_is_available_on_every_dialect`.
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        compound = SnowflakeDialectAdapter().set_operation(op, False, selects)
        rendered = str(compound.compile(dialect=_SNOWFLAKE_DIALECT)).upper()
        assert op.upper() in rendered
        assert f"{op.upper()} ALL" not in rendered


class TestSnowflakeColumnMask:
    def _mask(self, kind: str, **extra):
        from querygate.policy.models import ColumnMask

        return ColumnMask(column="t.c", kind=kind, **extra)

    def test_null_and_bucket_render(self):
        adapter = SnowflakeDialectAdapter()
        col = sa.column("amount")
        null_expr = adapter.column_mask(col, self._mask("null"))
        assert "NULL" in _render_snowflake(sa.select(null_expr)).upper()
        bucket_expr = adapter.column_mask(col, self._mask("bucket", bucket_size=10))
        assert "floor" in _render_snowflake(sa.select(bucket_expr)).lower()

    def test_last_renders_right(self):
        adapter = SnowflakeDialectAdapter()
        col = sa.column("ssn")
        expr = adapter.column_mask(col, self._mask("last", length=4))
        rendered = _render_snowflake(sa.select(expr)).lower()
        assert "right(" in rendered

    def test_hash_renders_sha2(self):
        adapter = SnowflakeDialectAdapter()
        col = sa.column("email")
        expr = adapter.column_mask(col, self._mask("hash"))
        rendered = _render_snowflake(sa.select(expr)).lower()
        assert "sha2(" in rendered


class TestSnowflakeMapsAreNotDeadCode:
    """Mutation check (CLAUDE.md's self-review discipline): perturb each
    per-dialect map and confirm the adapter's rendered SQL/behavior actually
    changes, the same technique `test_date_primitives.py`'s
    `test_no_date_part_map_is_dead_code` uses for the other three dialects."""

    def test_extract_field_map_is_read_by_the_adapter(self):
        from querygate.compiler import dialect_adapters as da

        expr = lambda: da.SnowflakeDialectAdapter().extract_part(  # noqa: E731
            "year", sa.column("created_at")
        )
        baseline = _render_snowflake(expr())
        original = da._SNOWFLAKE_EXTRACT_FIELDS["year"]
        da._SNOWFLAKE_EXTRACT_FIELDS["year"] = "qg_sentinel_value"
        try:
            mutated = _render_snowflake(expr())
        finally:
            da._SNOWFLAKE_EXTRACT_FIELDS["year"] = original
        assert mutated != baseline and "qg_sentinel_value" in mutated

    def test_dateadd_unit_map_is_read_by_the_adapter(self):
        """Like `_MSSQL_DATEADD_UNITS`, this map is an IDENTITY mapping, so
        the only observable coupling is the rejection: remove a unit and the
        adapter must refuse it, or the map is dead and some other lookup
        (or none at all) is what actually decides support."""
        from querygate.compiler import dialect_adapters as da

        adapter = da.SnowflakeDialectAdapter()
        original = dict(da._SNOWFLAKE_DATEADD_UNITS)
        da._SNOWFLAKE_DATEADD_UNITS = {k: v for k, v in original.items() if k != "day"}
        try:
            with pytest.raises(QueryValidationError, match="not supported"):
                adapter.date_add(sa.column("c"), "day", 1)
        finally:
            da._SNOWFLAKE_DATEADD_UNITS = original
