"""Unit tests for the DialectAdapter abstraction (TODO.md item 73/57).

`date_bucket` cases are a regression check: they must render byte-for-byte
identical to what `_date_bucket_expr`/`_sqlite_date_bucket_expr` produced
before this module existed, proving the extraction was behavior-neutral.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from querygate.compiler.dialect_adapters import (
    BigQueryDialectAdapter,
    DialectAdapter,
    MSSQLDialectAdapter,
    MySQLDialectAdapter,
    PostgresDialectAdapter,
    SnowflakeDialectAdapter,
    SQLiteDialectAdapter,
    get_dialect_adapter,
)
from querygate.core.exceptions import QueryValidationError
from querygate.policy.models import ColumnMask
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


# BigQuery cases throughout this file render against the REAL installed
# `sqlalchemy_bigquery` dialect object, the same "renders as documented, not
# confirmed correct against a real account" posture as the Snowflake cases
# above (TODO.md item 19 phase 3) — no BigQuery project/credentials are
# available in this environment. `sqlalchemy-bigquery` is a DEV-ONLY
# dependency for the identical reason `snowflake-sqlalchemy` is (see
# pyproject.toml): no production module imports it, since
# connections/engine.py's init_engine refuses to open a BigQuery connection
# at all. `importorskip`, matching the Snowflake pattern, so this file still
# collects cleanly in a hypothetical main-dependencies-only environment.
_sqlalchemy_bigquery = pytest.importorskip("sqlalchemy_bigquery")
_BIGQUERY_DIALECT = _sqlalchemy_bigquery.BigQueryDialect()


def _render_bigquery(expr) -> str:
    return str(expr.compile(dialect=_BIGQUERY_DIALECT, compile_kwargs={"literal_binds": True}))


def _bq_col(name: str, type_=None):
    """A BigQuery-typed column reference. `date_bucket`/`date_add` dispatch
    on the operand's concrete SQLAlchemy type (see `_bq_temporal_kind`'s
    docstring in dialect_adapters.py) — unlike every other adapter's
    plain `sa.column(name)`, an untyped reference renders nothing useful for
    those two methods, only for the type-agnostic ones (extract_part,
    scalar_function, ...)."""
    return sa.column(name, type_=type_) if type_ is not None else sa.column(name)


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

    @pytest.mark.parametrize("granularity", ["day", "week", "month", "quarter", "year"])
    def test_bigquery_supports_every_granularity(self, granularity):
        # BigQuery's DATETIME_TRUNC covers every granularity directly
        # (documented in Google's docs), unlike Postgres it needs a
        # concretely-typed operand — see TestBigQueryTemporalKindDispatch for
        # the three-way DATE/DATETIME/TIMESTAMP dispatch this exercises.
        col = _bq_col("created_at", sa.DateTime)
        expr = BigQueryDialectAdapter().date_bucket(col, granularity)
        _render_bigquery(expr)  # must not raise

    def test_bigquery_week_uses_isoweek(self):
        col = _bq_col("created_at", sa.DateTime)
        rendered = _render_bigquery(BigQueryDialectAdapter().date_bucket(col, "week")).lower()
        assert "isoweek" in rendered

    def test_bigquery_rejects_unsupported_granularity(self):
        col = _bq_col("created_at", sa.DateTime)
        with pytest.raises(QueryValidationError, match="Unsupported date_bucket granularity"):
            BigQueryDialectAdapter().date_bucket(col, "decade")


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

    def test_bigquery_no_nulls_returns_single_term(self):
        col = sa.column("status")
        terms = BigQueryDialectAdapter().order_by_terms(col, "desc", None)
        assert len(terms) == 1

    @pytest.mark.parametrize("nulls", ["first", "last"])
    def test_bigquery_nulls_uses_native_clause(self, nulls):
        # Like Snowflake, BigQuery supports NULLS FIRST/LAST natively
        # (Google's Query syntax reference) — the Postgres/SQLite shape.
        col = sa.column("status")
        terms = BigQueryDialectAdapter().order_by_terms(col, "asc", nulls)
        assert len(terms) == 1
        assert f"NULLS {nulls.upper()}" in _render_bigquery(terms[0]).upper()


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

    def test_bigquery_stddev_variance_render_plain_names(self):
        # BigQuery's bare STDDEV/VARIANCE ARE documented as aliases of
        # STDDEV_SAMP/VAR_SAMP — the Postgres/Snowflake shape, not MySQL's
        # population-default gap. Exact-name assertions (not `"stddev" in
        # rendered`), the same substring-collision discipline as
        # Snowflake's case above: "stddev" is a substring of "stddev_samp",
        # so a plain-substring check would stay green even if this branch
        # were accidentally copied from MySQL's `stddev_samp` mapping.
        col = sa.column("amount")
        rendered_std = _render_bigquery(BigQueryDialectAdapter().stat_fn("stddev")(col)).lower()
        rendered_var = _render_bigquery(BigQueryDialectAdapter().stat_fn("variance")(col)).lower()
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

    def test_bigquery_renders_string_agg(self):
        # STRING_AGG(expr, delimiter) — the same 2-argument comma shape as
        # Postgres's string_agg/Snowflake's LISTAGG. BigQuery's compiler
        # backtick-quotes identifiers (`` `email` ``), unlike the other
        # dialects here, so the assertion checks for the quoted form.
        col = sa.column("email")
        rendered = _render_bigquery(BigQueryDialectAdapter().string_agg(col, ", ")).lower()
        assert "string_agg(`email`" in rendered


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

    def test_bigquery_renders_array_agg(self):
        # Like Snowflake, BigQuery's ARRAY_AGG returns a genuine native
        # ARRAY<T> type — a real equivalent, so this is a real render, not a
        # rejection.
        col = sa.column("status")
        rendered = _render_bigquery(BigQueryDialectAdapter().array_agg(col)).lower()
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
        "bigquery": BigQueryDialectAdapter(),
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

    @pytest.mark.parametrize("name", ["postgres", "mysql", "sqlite", "snowflake", "bigquery"])
    def test_numeric_range_offsets_are_supported_off_mssql(self, name):
        # Snowflake's RANGE BETWEEN with a numeric offset reached General
        # Availability 2024-08-08 per Snowflake's release notes; BigQuery's
        # window-function-calls reference documents `numeric_preceding`/
        # `numeric_following` as part of its frame grammar directly — this is
        # a rendering assertion only; an account/project on an older release
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


class TestBigQueryTemporalKindDispatch:
    """BigQuery is the one adapter whose date_bucket/date_add genuinely need
    to know the operand's concrete type (see `_bq_temporal_kind`'s docstring
    in dialect_adapters.py) — no other dialect's adapter has this axis, so
    it gets its own test class rather than folding into the extract/date_add
    class below."""

    def test_date_kind_uses_date_trunc_and_date_add(self):
        adapter = BigQueryDialectAdapter()
        col = _bq_col("d", sa.Date)
        bucket = _render_bigquery(adapter.date_bucket(col, "month"))
        assert "date_trunc" in bucket.lower()
        assert "datetime_trunc" not in bucket.lower()
        assert "timestamp_trunc" not in bucket.lower()
        added = adapter.date_add(col, "month", 1)
        compiled = str(added.compile(dialect=_BIGQUERY_DIALECT))
        assert "date_add" in compiled.lower()
        assert "datetime_add" not in compiled.lower()
        assert "timestamp_add" not in compiled.lower()

    def test_datetime_kind_uses_datetime_trunc_and_datetime_add(self):
        adapter = BigQueryDialectAdapter()
        col = _bq_col("dt", sa.DateTime)
        bucket = _render_bigquery(adapter.date_bucket(col, "month"))
        assert "datetime_trunc" in bucket.lower()
        added = adapter.date_add(col, "hour", 1)
        compiled = str(added.compile(dialect=_BIGQUERY_DIALECT))
        assert "datetime_add" in compiled.lower()

    def test_timestamp_kind_uses_timestamp_trunc_and_timestamp_add(self):
        adapter = BigQueryDialectAdapter()
        col = _bq_col("ts", sa.TIMESTAMP)
        bucket = _render_bigquery(adapter.date_bucket(col, "month"))
        assert "timestamp_trunc" in bucket.lower()
        added = adapter.date_add(col, "hour", 1)
        compiled = str(added.compile(dialect=_BIGQUERY_DIALECT))
        assert "timestamp_add" in compiled.lower()

    def test_timestamp_is_not_misclassified_as_datetime(self):
        """The isinstance-ordering bug this dispatch would fall into if
        checked in the wrong order: `sa.TIMESTAMP` IS a subclass of
        `sa.DateTime`, so checking `isinstance(t, sa.DateTime)` first would
        silently route every TIMESTAMP column through DATETIME_TRUNC/
        DATETIME_ADD instead — which, for date_add, would WRONGLY accept
        week/month/year units DATETIME_ADD supports but TIMESTAMP_ADD does
        not (a silent capability-gap miss, not a syntax error)."""
        adapter = BigQueryDialectAdapter()
        col = _bq_col("ts", sa.TIMESTAMP)
        # TIMESTAMP_ADD genuinely lacks 'week' (see TestBigQueryDateAddTypeGaps
        # below) — if this misclassified as DATETIME it would render instead
        # of raising.
        with pytest.raises(QueryValidationError, match="TIMESTAMP_ADD"):
            adapter.date_add(col, "week", 1)

    def test_untyped_operand_is_rejected_not_guessed(self):
        """An expression whose SQLAlchemy type cannot be determined (NullType
        — e.g. a bare `sa.column(...)` with no `type_`) must raise a named,
        typed error, never silently pick a kind."""
        adapter = BigQueryDialectAdapter()
        untyped = sa.column("mystery")
        with pytest.raises(QueryValidationError, match="could not determine"):
            adapter.date_bucket(untyped, "month")
        with pytest.raises(QueryValidationError, match="could not determine"):
            adapter.date_add(untyped, "day", 1)

    def test_date_bucket_and_date_add_results_carry_type_for_nesting(self):
        """current_timestamp/date_bucket/date_add each type_coerce their
        result so a caller can nest date_add(date_bucket(x), ...) or
        date_add(current_timestamp(...), ...) and still get correct dispatch
        at the next level — a bare `sa.func.x(...)` call defaults to
        NullType, which would make any such nesting hit the
        "could not determine" rejection above."""
        adapter = BigQueryDialectAdapter()
        now_ts = adapter.current_timestamp("timestamp")
        assert isinstance(now_ts.type, sa.TIMESTAMP)
        nested = adapter.date_add(now_ts, "hour", -1)
        # Renders via TIMESTAMP_ADD (not "could not determine ...") — proves
        # the type survived current_timestamp -> date_add.
        compiled = str(nested.compile(dialect=_BIGQUERY_DIALECT))
        assert "timestamp_add" in compiled.lower()

        bucketed = adapter.date_bucket(_bq_col("dt", sa.DateTime), "day")
        assert isinstance(bucketed.type, sa.DateTime)
        renested = adapter.date_add(bucketed, "week", 1)
        compiled2 = str(renested.compile(dialect=_BIGQUERY_DIALECT))
        assert "datetime_add" in compiled2.lower()


class TestBigQueryDateAddTypeGaps:
    """The reject-don't-emulate cases unique to BigQuery's three-way type
    split — a DATE has no time-of-day (so hour/minute/second are refused)
    and a TIMESTAMP is a timezone-independent instant (so week/month/quarter/
    year are refused), confirmed against Google's DATE_ADD/TIMESTAMP_ADD
    docs, not assumed."""

    @pytest.mark.parametrize("unit", ["hour", "minute", "second"])
    def test_date_operand_rejects_sub_day_units(self, unit):
        adapter = BigQueryDialectAdapter()
        with pytest.raises(QueryValidationError, match="DATE_ADD"):
            adapter.date_add(_bq_col("d", sa.Date), unit, 1)

    @pytest.mark.parametrize("unit", ["week", "month", "year"])
    def test_timestamp_operand_rejects_calendar_units(self, unit):
        adapter = BigQueryDialectAdapter()
        with pytest.raises(QueryValidationError, match="TIMESTAMP_ADD"):
            adapter.date_add(_bq_col("ts", sa.TIMESTAMP), unit, 1)

    def test_datetime_operand_has_no_gaps(self):
        """DATETIME_ADD supports the full IntervalUnit range — the one
        BigQuery kind with no genuine gap."""
        adapter = BigQueryDialectAdapter()
        for unit in IntervalUnit.__args__:
            expr = adapter.date_add(_bq_col("dt", sa.DateTime), unit, 1)
            assert _render_bigquery(expr)


class TestBigQueryExtractPartDateAddCurrentTimestamp:
    def test_every_date_part_is_supported(self):
        # Iterates the live `DatePart` enum directly, for the same reason
        # the Snowflake class above does. BigQuery has NO declared gaps
        # (extract_part needs no type dispatch — EXTRACT accepts DATE/
        # DATETIME/TIMESTAMP alike), so an untyped column is fine here.
        for part in DatePart.__args__:
            expr = BigQueryDialectAdapter().extract_part(part, sa.column("created_at"))
            assert _render_bigquery(expr)

    def test_dayofweek_is_extracted_and_normalized_modulo_sunday_first(self):
        # EXTRACT(DAYOFWEEK FROM x) returns [1,7] with Sunday=1 (Google's
        # docs); subtracting 1 gives the 0=Sunday..6=Saturday contract.
        rendered = _render_bigquery(
            BigQueryDialectAdapter().extract_part("dayofweek", sa.column("created_at"))
        ).lower()
        assert "dayofweek" in rendered
        assert "- 1" in rendered

    def test_week_uses_isoweek_not_the_sunday_start_week(self):
        rendered = _render_bigquery(
            BigQueryDialectAdapter().extract_part("week", sa.column("created_at"))
        ).lower()
        assert "isoweek" in rendered

    def test_unknown_part_raises_a_typed_error_not_a_keyerror(self):
        with pytest.raises(QueryValidationError, match="not supported"):
            BigQueryDialectAdapter().extract_part("nanocentury", sa.column("c"))

    def test_date_add_renders_every_unit_on_a_datetime_operand(self):
        # `IntervalUnit.__args__` directly, for the same reason
        # `test_every_date_part_is_supported` above iterates `DatePart`
        # rather than a hand-copied tuple. A DATETIME operand is used
        # because it is the one BigQuery kind with no genuine unit gap
        # (see TestBigQueryDateAddTypeGaps) — DATE/TIMESTAMP each reject a
        # real subset, covered separately above.
        for unit in IntervalUnit.__args__:
            expr = BigQueryDialectAdapter().date_add(_bq_col("created_at", sa.DateTime), unit, 1)
            assert _render_bigquery(expr)

    def test_date_add_unknown_unit_raises_a_typed_error(self):
        with pytest.raises(QueryValidationError, match="not supported"):
            BigQueryDialectAdapter().date_add(_bq_col("c", sa.DateTime), "nanocentury", 3)

    def test_date_add_binds_the_amount_rather_than_inlining_it(self):
        # `sa.text(...).bindparams(amt=amount)` is a BOUND PARAMETER, not
        # text interpolation — asserted by compiling WITHOUT literal_binds
        # (the default) and checking the amount does NOT appear in the SQL
        # text itself, only in the compiled statement's params.
        expr = BigQueryDialectAdapter().date_add(_bq_col("created_at", sa.DateTime), "day", -7)
        compiled = expr.compile(dialect=_BIGQUERY_DIALECT)
        assert "-7" not in str(compiled), "the amount must bind, not be inlined into SQL text"
        assert -7 in compiled.params.values()

    def test_current_timestamp_uses_current_timestamp_function(self):
        rendered = _render_bigquery(BigQueryDialectAdapter().current_timestamp("timestamp"))
        assert "CURRENT_TIMESTAMP" in rendered.upper()

    def test_current_timestamp_date_kind_casts_to_a_real_date_type(self):
        sql = _render_bigquery(BigQueryDialectAdapter().current_timestamp("date"))
        cast_target = sql.upper().rsplit(" AS ", 1)[1].rstrip(")")
        assert cast_target == "DATE", f"expected a DATE cast, got {cast_target!r}"


class TestBigQueryScalarFunctions:
    def test_ceil_length_substring_round(self):
        adapter = BigQueryDialectAdapter()
        col = sa.column("x")
        assert "ceil" in _render_bigquery(adapter.scalar_function("ceil", [col])).lower()
        assert "length" in _render_bigquery(adapter.scalar_function("length", [col])).lower()
        rendered_sub = _render_bigquery(
            adapter.scalar_function("substring", [col, sa.literal(1), sa.literal(3)])
        ).lower()
        assert "substr" in rendered_sub
        assert "round" in _render_bigquery(adapter.scalar_function("round", [col])).lower()
        rendered_round2 = _render_bigquery(
            adapter.scalar_function("round", [col, sa.literal(2)])
        ).lower()
        assert "round" in rendered_round2

    def test_unsupported_scalar_function_is_rejected(self):
        with pytest.raises(QueryValidationError, match="Unsupported scalar function"):
            BigQueryDialectAdapter().scalar_function("not_a_real_function", [sa.column("x")])


class TestBigQuerySetOperation:
    def test_union_all_is_supported(self):
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        compound = BigQueryDialectAdapter().set_operation("union", True, selects)
        assert "UNION ALL" in str(compound.compile(dialect=_BIGQUERY_DIALECT)).upper()

    @pytest.mark.parametrize("op", ["intersect", "except"])
    def test_intersect_except_all_are_rejected_not_emulated(self, op):
        # BigQuery's INTERSECT/EXCEPT are distinct-only — the same genuine
        # gap as MSSQL/MySQL/Snowflake, not a spelling difference.
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        with pytest.raises(QueryValidationError, match=f"{op.upper()} ALL is not supported"):
            BigQueryDialectAdapter().set_operation(op, True, selects)

    @pytest.mark.parametrize("op", ["intersect", "except"])
    def test_intersect_except_without_all_are_supported(self, op):
        # Asserts the RENDERED KEYWORD, not just "returned something" — same
        # discipline as the Snowflake case above. BigQuery's compiler injects
        # an explicit DISTINCT keyword (confirmed by compiling against the
        # installed dialect object) rather than a bare keyword, so this also
        # guards that the DISTINCT form is what's actually produced.
        selects = [sa.select(sa.literal_column("1")), sa.select(sa.literal_column("2"))]
        compound = BigQueryDialectAdapter().set_operation(op, False, selects)
        rendered = str(compound.compile(dialect=_BIGQUERY_DIALECT)).upper()
        assert op.upper() in rendered
        assert f"{op.upper()} ALL" not in rendered


class TestBigQueryPercentileContRejected:
    def test_percentile_cont_is_rejected_as_a_group_by_aggregate(self):
        # BigQuery's PERCENTILE_CONT syntax requires OVER(...) — there is no
        # GROUP BY-compatible form, the same genuine gap as MSSQL's.
        with pytest.raises(QueryValidationError, match="navigation/window function"):
            BigQueryDialectAdapter().percentile_cont(sa.column("x"), 0.5)


class TestBigQueryColumnMask:
    def _mask(self, kind: str, **extra):
        from querygate.policy.models import ColumnMask

        return ColumnMask(column="t.c", kind=kind, **extra)

    def test_null_and_bucket_render(self):
        adapter = BigQueryDialectAdapter()
        col = sa.column("amount")
        null_expr = adapter.column_mask(col, self._mask("null"))
        assert "NULL" in _render_bigquery(sa.select(null_expr)).upper()
        bucket_expr = adapter.column_mask(col, self._mask("bucket", bucket_size=10))
        assert "floor" in _render_bigquery(sa.select(bucket_expr)).lower()

    def test_last_renders_right(self):
        adapter = BigQueryDialectAdapter()
        col = sa.column("ssn")
        expr = adapter.column_mask(col, self._mask("last", length=4))
        rendered = _render_bigquery(sa.select(expr)).lower()
        assert "right(" in rendered

    def test_hash_renders_to_hex_sha256(self):
        adapter = BigQueryDialectAdapter()
        col = sa.column("email")
        expr = adapter.column_mask(col, self._mask("hash"))
        rendered = _render_bigquery(sa.select(expr)).lower()
        assert "to_hex(" in rendered
        assert "sha256(" in rendered


class TestBigQueryMapsAreNotDeadCode:
    """Mutation check (CLAUDE.md's self-review discipline) — same technique
    as TestSnowflakeMapsAreNotDeadCode above."""

    def test_extract_field_map_is_read_by_the_adapter(self):
        from querygate.compiler import dialect_adapters as da

        expr = lambda: da.BigQueryDialectAdapter().extract_part(  # noqa: E731
            "year", sa.column("created_at")
        )
        baseline = _render_bigquery(expr())
        original = da._BQ_EXTRACT_FIELDS["year"]
        da._BQ_EXTRACT_FIELDS["year"] = "qg_sentinel_value"
        try:
            mutated = _render_bigquery(expr())
        finally:
            da._BQ_EXTRACT_FIELDS["year"] = original
        assert mutated != baseline and "qg_sentinel_value" in mutated

    def test_trunc_keyword_map_is_read_by_the_adapter(self):
        from querygate.compiler import dialect_adapters as da

        adapter = da.BigQueryDialectAdapter()
        col = _bq_col("dt", sa.DateTime)
        baseline = _render_bigquery(adapter.date_bucket(col, "month"))
        original = da._BQ_TRUNC_KEYWORDS["month"]
        da._BQ_TRUNC_KEYWORDS["month"] = "QG_SENTINEL"
        try:
            mutated = _render_bigquery(adapter.date_bucket(col, "month"))
        finally:
            da._BQ_TRUNC_KEYWORDS["month"] = original
        assert mutated != baseline and "QG_SENTINEL" in mutated

    def test_datetime_add_unit_map_is_read_by_the_adapter(self):
        """IDENTITY-shaped mapping (same as `_MSSQL_DATEADD_UNITS`), so the
        only observable coupling is the rejection: remove a unit and the
        adapter must refuse it."""
        from querygate.compiler import dialect_adapters as da

        adapter = da.BigQueryDialectAdapter()
        original = dict(da._BQ_DATETIME_ADD_UNITS)
        da._BQ_DATETIME_ADD_UNITS = {k: v for k, v in original.items() if k != "day"}
        try:
            with pytest.raises(QueryValidationError, match="not supported"):
                adapter.date_add(_bq_col("dt", sa.DateTime), "day", 1)
        finally:
            da._BQ_DATETIME_ADD_UNITS = original

    def test_date_add_unit_map_is_read_by_the_adapter(self):
        """The sibling of the DATETIME test above for `_BQ_DATE_ADD_UNITS` —
        each of the three per-kind unit maps is a direct module-global
        reference inside `date_add`, not a single shared indirection dict
        (see the code comment on that choice), so each needs its own
        mutation pin or a mutation to one specific map could go unnoticed."""
        from querygate.compiler import dialect_adapters as da

        adapter = da.BigQueryDialectAdapter()
        original = dict(da._BQ_DATE_ADD_UNITS)
        da._BQ_DATE_ADD_UNITS = {k: v for k, v in original.items() if k != "day"}
        try:
            with pytest.raises(QueryValidationError, match="not supported"):
                adapter.date_add(_bq_col("d", sa.Date), "day", 1)
        finally:
            da._BQ_DATE_ADD_UNITS = original

    def test_timestamp_add_unit_map_is_read_by_the_adapter(self):
        """The `_BQ_TIMESTAMP_ADD_UNITS` sibling of the two tests above."""
        from querygate.compiler import dialect_adapters as da

        adapter = da.BigQueryDialectAdapter()
        original = dict(da._BQ_TIMESTAMP_ADD_UNITS)
        da._BQ_TIMESTAMP_ADD_UNITS = {k: v for k, v in original.items() if k != "day"}
        try:
            with pytest.raises(QueryValidationError, match="not supported"):
                adapter.date_add(_bq_col("ts", sa.TIMESTAMP), "day", 1)
        finally:
            da._BQ_TIMESTAMP_ADD_UNITS = original


# --------------------------------------------------------------------------- #
# column_mask exhaustiveness (TODO.md item 164)
# --------------------------------------------------------------------------- #
# `column_mask` used to end every adapter on an unconditional final branch
# that assumed HASH — an implicit else, not an exhaustive match against
# `ColumnMaskKind` — unlike this module's date-part/interval-unit maps, which
# are all deliberately exhaustive so a future enum member is a forced
# decision, never a silent passthrough. This section pins both halves of the
# fix: real members still render exactly as before (no behavior change), and
# an unrecognized kind is now rejected with a typed QueryValidationError
# naming the dialect, on all five registered adapters, instead of silently
# rendering as a full HASH transform.
_COLUMN_MASK_DIALECT_CASES = [
    ("postgresql", _render, "md5("),
    ("mssql", _render, "HASHBYTES"),
    ("mysql", _render, "sha2("),
    ("snowflake", _render_snowflake, "sha2("),
    ("bigquery", _render_bigquery, "sha256("),
]


class TestColumnMaskExhaustiveness:
    @pytest.mark.parametrize(
        "dialect_name,render,_hash_substr",
        _COLUMN_MASK_DIALECT_CASES,
        ids=[c[0] for c in _COLUMN_MASK_DIALECT_CASES],
    )
    def test_null_renders(self, dialect_name, render, _hash_substr):
        adapter = get_dialect_adapter(dialect_name)
        col = sa.column("phone")
        expr = adapter.column_mask(col, ColumnMask(column="phone", kind="null"))
        assert "NULL" in render(sa.select(expr)).upper()

    @pytest.mark.parametrize(
        "dialect_name,render,_hash_substr",
        _COLUMN_MASK_DIALECT_CASES,
        ids=[c[0] for c in _COLUMN_MASK_DIALECT_CASES],
    )
    def test_bucket_renders(self, dialect_name, render, _hash_substr):
        adapter = get_dialect_adapter(dialect_name)
        col = sa.column("salary")
        expr = adapter.column_mask(
            col, ColumnMask(column="salary", kind="bucket", bucket_size=1000)
        )
        assert "floor" in render(sa.select(expr)).lower()

    @pytest.mark.parametrize(
        "dialect_name,render,_hash_substr",
        _COLUMN_MASK_DIALECT_CASES,
        ids=[c[0] for c in _COLUMN_MASK_DIALECT_CASES],
    )
    def test_last_renders(self, dialect_name, render, _hash_substr):
        adapter = get_dialect_adapter(dialect_name)
        col = sa.column("phone")
        expr = adapter.column_mask(col, ColumnMask(column="phone", kind="last", length=4))
        assert "right(" in render(sa.select(expr)).lower()

    @pytest.mark.parametrize(
        "dialect_name,render,hash_substr",
        _COLUMN_MASK_DIALECT_CASES,
        ids=[c[0] for c in _COLUMN_MASK_DIALECT_CASES],
    )
    def test_hash_renders_dialect_native_idiom(self, dialect_name, render, hash_substr):
        adapter = get_dialect_adapter(dialect_name)
        col = sa.column("email")
        expr = adapter.column_mask(col, ColumnMask(column="email", kind="hash"))
        rendered = render(sa.select(expr))
        assert hash_substr.lower() in rendered.lower()

    @pytest.mark.parametrize(
        "dialect_name",
        [c[0] for c in _COLUMN_MASK_DIALECT_CASES],
    )
    def test_unrecognized_kind_raises_instead_of_rendering_as_hash(self, dialect_name):
        """The actual regression this item closes: before the fix, an
        unrecognized `mask.kind` fell through every adapter's final branch
        and rendered as a full HASH transform. `ColumnMask.kind` is a real
        pydantic-validated `ColumnMaskKind` field, so a bogus value can't be
        constructed through the model's normal `__init__` — use
        `model_construct` (the established validation-skipping idiom already
        used for this exact purpose in `query_ast/models.py`/
        `write_ast/models.py`) to reach the adapter with a value none of the
        `is` checks can match, the same way a future 5th enum member would.
        Deliberately not `mask.kind = "..."` post-construction assignment:
        that only stays a validation bypass because `ColumnMask` doesn't set
        `validate_assignment=True` today — a future hardening pass adding it
        would make the assignment itself raise `pydantic.ValidationError`
        before ever reaching the adapter, silently stopping this test from
        covering the guard at all rather than failing loudly."""
        adapter = get_dialect_adapter(dialect_name)
        mask = ColumnMask.model_construct(column="email", kind="qg_sentinel_mask_kind")
        with pytest.raises(QueryValidationError, match="qg_sentinel_mask_kind"):
            adapter.column_mask(sa.column("email"), mask)
