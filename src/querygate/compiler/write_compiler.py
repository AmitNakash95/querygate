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
from typing import Any, Dict

import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
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
    except ValueError:
        return value
    return value


def _coerce_row(row: Dict[str, Any], table: sa.Table) -> Dict[str, Any]:
    return {
        key: (_coerce_write_value(table.c[key], val) if key in table.c else val)
        for key, val in row.items()
    }


def compile_write(statement: WriteStatement, table: sa.Table) -> sa.sql.expression.Executable:
    """Build the Core DML statement. `table` is the reflected target table; the
    WHERE (for update/delete) resolves its `table.column` refs against it."""
    tables = {table.name: table}

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

    raise TypeError(f"Unknown write statement type: {type(statement).__name__}")
