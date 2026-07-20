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

    def test_mssql_nulls_emulated_via_case_bucket_not_native_syntax(self):
        col = sa.column("status")
        terms = MSSQLDialectAdapter().order_by_terms(col, "asc", "last")
        assert len(terms) == 2
        rendered = _render(terms[0]).upper()
        assert "CASE" in rendered
        assert "NULLS" not in rendered  # never emit the syntax T-SQL doesn't support

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
