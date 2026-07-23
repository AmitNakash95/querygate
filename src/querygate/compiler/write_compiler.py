"""Compile a validated `WriteStatement` into a SQLAlchemy Core DML statement
(TODO.md item 93, Phase 1) — the write sibling of `sqlalchemy_compiler.py`.

There is no string assembly: an INSERT/UPDATE/DELETE is built from SQLAlchemy
Core constructs against the reflected `sa.Table`, and the WHERE clause reuses the
read compiler's `_compile_where` verbatim — the same bound-parameter, no-raw-SQL
path reads use. In Phase 1 the result is only ever run inside a rolled-back
preview transaction, never committed.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal as _Decimal
from decimal import InvalidOperation as _InvalidOperation
from typing import Any, Dict

import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.core.exceptions import QueryValidationError
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
    WriteStatement,
)


def _coerce_write_value(column: sa.Column, value: Any) -> Any:
    """Coerce a JSON string into the Python type a temporal column binds.

    A write value arrives as JSON, so a timestamp/date/time is a string — but a
    typed DateTime/Date/Time column's driver binds a Python `datetime`/`date`/
    `time` object, not a string (SQLite rejects the string outright; other
    drivers vary). Coerce those here so an INSERT/UPDATE of a temporal column
    works; anything that isn't an ISO temporal string is passed through
    untouched, so a genuinely bad value still surfaces as a clean DB error
    rather than being guessed at. Non-temporal types (int/decimal/bool/str) bind
    from JSON directly and are left alone."""
    if not isinstance(value, str):
        return value
    try:
        pytype = column.type.python_type
    except NotImplementedError:
        return value
    try:
        if pytype is _dt.datetime:
            return _dt.datetime.fromisoformat(value)
        if pytype is _dt.date:
            return _dt.date.fromisoformat(value)
        if pytype is _dt.time:
            return _dt.time.fromisoformat(value)
        if pytype is _Decimal:
            # A Numeric column's driver (e.g. asyncpg) wants a Decimal, not a
            # string — matters when a value arrives through a JSON request body
            # that carried the number as a string.
            return _Decimal(value)
    except (ValueError, _InvalidOperation):
        return value
    return value


def _coerce_row(row: Dict[str, Any], table: sa.Table) -> Dict[str, Any]:
    return {
        key: (_coerce_write_value(table.c[key], val) if key in table.c else val)
        for key, val in row.items()
    }


def compile_write(
    statement: WriteStatement, table: sa.Table, dialect: str = "postgresql"
) -> sa.sql.expression.Executable:
    """Build the Core DML statement. `table` is the reflected target table; the
    WHERE (for update/delete) resolves its `table.column` refs against it.
    `dialect` selects the upsert idiom (ignored for insert/update/delete, which
    are dialect-agnostic Core)."""
    if isinstance(statement, InsertStatement):
        return sa.insert(table).values([_coerce_row(row, table) for row in statement.rows])

    if isinstance(statement, UpdateStatement):
        # A validated set: bare column name -> literal value. `.where` is
        # required by the AST, so this is never an unqualified UPDATE.
        where_clause = _compile_where(statement.where, {statement.table: table}, alias_map={})
        return sa.update(table).where(where_clause).values(**_coerce_row(statement.set, table))

    if isinstance(statement, DeleteStatement):
        where_clause = _compile_where(statement.where, {statement.table: table}, alias_map={})
        return sa.delete(table).where(where_clause)

    if isinstance(statement, UpsertStatement):
        return _compile_upsert(statement, table, dialect)

    raise TypeError(f"Unknown write statement type: {type(statement).__name__}")


def _on_conflict_upsert(dialect_insert):
    """Build an upsert compiler for a dialect whose Core `insert()` has the
    `on_conflict_do_update` construct (Postgres, SQLite)."""

    def _compile(statement: UpsertStatement, table: sa.Table):
        stmt = dialect_insert(table).values([_coerce_row(row, table) for row in statement.rows])
        return stmt.on_conflict_do_update(
            index_elements=statement.conflict_columns,
            set_={col: stmt.excluded[col] for col in statement.update_columns},
        )

    return _compile


def _pg_upsert_insert():
    from sqlalchemy.dialects.postgresql import insert

    return insert


def _sqlite_upsert_insert():
    from sqlalchemy.dialects.sqlite import insert

    return insert


# Per-dialect upsert compiler registry (composable-interface doctrine — no inline
# `if dialect == ...`). A dialect absent here has no ON CONFLICT and is rejected
# rather than emulated (item-74 reject-not-emulate): MSSQL's MERGE is deliberately
# not synthesized on the caller's behalf.
_UPSERT_COMPILERS = {
    "postgresql": lambda: _on_conflict_upsert(_pg_upsert_insert()),
    "sqlite": lambda: _on_conflict_upsert(_sqlite_upsert_insert()),
}


def _compile_upsert(
    statement: UpsertStatement, table: sa.Table, dialect: str
) -> sa.sql.expression.Executable:
    """INSERT ... ON CONFLICT DO UPDATE, dispatched by dialect through the
    registry. A dialect without a native ON CONFLICT (MSSQL) is rejected."""
    factory = _UPSERT_COMPILERS.get(dialect)
    if factory is None:
        raise QueryValidationError(
            f"upsert (INSERT ON CONFLICT) is not supported on dialect {dialect!r} — it has no "
            "ON CONFLICT clause. Use a separate governed update then insert, or preview which "
            "rows exist first."
        )
    return factory()(statement, table)
