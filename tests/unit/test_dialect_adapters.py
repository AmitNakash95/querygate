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
    PostgresDialectAdapter,
    SQLiteDialectAdapter,
    get_dialect_adapter,
)
from querygate.core.exceptions import QueryValidationError


def _render(expr) -> str:
    return str(expr.compile(compile_kwargs={"literal_binds": True}))


class TestGetDialectAdapter:
    def test_postgresql_and_mssql_resolve_to_their_own_adapter(self):
        assert isinstance(get_dialect_adapter("postgresql"), PostgresDialectAdapter)
        assert isinstance(get_dialect_adapter("mssql"), MSSQLDialectAdapter)

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


class TestWindowFrame:
    """item 101 — the frame grammar is the one genuinely per-dialect part of a
    window, so it is the one part that lives on the adapter."""

    _ADAPTERS = {
        "postgres": PostgresDialectAdapter(),
        "mssql": MSSQLDialectAdapter(),
        "sqlite": SQLiteDialectAdapter(),
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

    @pytest.mark.parametrize("name", ["postgres", "sqlite"])
    def test_numeric_range_offsets_are_supported_off_mssql(self, name):
        assert self._ADAPTERS[name].window_frame("range", -6, 0) == {"range_": (-6, 0)}
