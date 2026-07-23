"""Compile a validated `WriteStatement` into a SQLAlchemy Core DML statement
(TODO.md item 93, Phase 1) — the write sibling of `sqlalchemy_compiler.py`.

There is no string assembly: an INSERT/UPDATE/DELETE is built from SQLAlchemy
Core constructs against the reflected `sa.Table`, and the WHERE clause reuses the
read compiler's `_compile_where` verbatim — the same bound-parameter, no-raw-SQL
path reads use. In Phase 1 the result is only ever run inside a rolled-back
preview transaction, never committed.
"""

from __future__ import annotations

import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    WriteStatement,
)


def compile_write(statement: WriteStatement, table: sa.Table) -> sa.sql.expression.Executable:
    """Build the Core DML statement. `table` is the reflected target table; the
    WHERE (for update/delete) resolves its `table.column` refs against it."""
    tables = {table.name: table}

    if isinstance(statement, InsertStatement):
        return sa.insert(table).values(statement.rows)

    if isinstance(statement, UpdateStatement):
        # A validated set: bare column name -> literal value. `.where` is
        # required by the AST, so this is never an unqualified UPDATE.
        where_clause = _compile_where(statement.where, {statement.table: table}, alias_map={})
        return sa.update(table).where(where_clause).values(**statement.set)

    if isinstance(statement, DeleteStatement):
        where_clause = _compile_where(statement.where, {statement.table: table}, alias_map={})
        return sa.delete(table).where(where_clause)

    raise TypeError(f"Unknown write statement type: {type(statement).__name__}")
