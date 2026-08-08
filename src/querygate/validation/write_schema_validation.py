"""Write schema-truth validation (TODO.md item 93, Phase 1) — the write sibling
of `validation/schema_validation.py`. Reflects the single target table and
verifies every written column and every WHERE-clause column reference exists,
returning the reflected `sa.Table` for the write compiler.

Patch the module-level `_load_table` (re-exported from schema_validation) to
unit-test without a real database, exactly as for reads.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.validation.schema_validation import (
    _load_table,
    iter_where_predicates,
    parse_column_ref,
    predicate_column_refs,
    resolve_column,
)
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
    WriteStatement,
    to_read_where,
)


async def validate_write_schema(
    statement: WriteStatement, connection_id: str, principal: Optional[Principal] = None
) -> sa.Table:
    """Reflect the target table and verify every written column and WHERE ref
    exists. Returns the reflected table for the compiler."""
    table = await _load_table(connection_id, statement.table, connection_id)

    # Written columns exist (insert/upsert rows / update set).
    if isinstance(statement, (InsertStatement, UpsertStatement)):
        written = statement.rows[0].keys()
    elif isinstance(statement, UpdateStatement):
        written = statement.set.keys()
    else:
        written = []
    for column in written:
        resolve_column(table, column)  # raises QueryValidationError if missing

    # For an INSERT, a NOT NULL column with no default that the row omits would
    # fail at the database as an opaque IntegrityError — catch it here as a clean,
    # precise validation error instead. Auto-generated primary keys are exempt
    # (the DB fills them), so this flags the real "you forgot a required field"
    # case (a NOT NULL FK/business column) without false-positiving on a surrogate
    # id column.
    if isinstance(statement, InsertStatement):
        provided = set(statement.rows[0].keys())
        missing = sorted(
            col.name
            for col in table.columns
            if not col.nullable
            and col.default is None
            and col.server_default is None
            and not col.primary_key
            and col.name not in provided
        )
        if missing:
            raise QueryValidationError(
                f"INSERT into {table.name!r} is missing required column(s): {', '.join(missing)}"
            )

    # WHERE refs must exist AND must reference the single target table (a write
    # is single-table in Phase 1 — no correlated/other-table refs).
    where = getattr(statement, "where", None)
    if where is not None:
        # Narrowed write filter -> read WhereNode (item 114); see write_ast.models.
        for pred in iter_where_predicates(to_read_where(where)):
            for ref in predicate_column_refs(pred):
                ref_table, ref_col = parse_column_ref(ref)
                if ref_table.casefold() != statement.table.casefold():
                    raise QueryValidationError(
                        f"A write's WHERE may only reference its target table "
                        f"{statement.table!r}, not {ref_table!r}"
                    )
                resolve_column(table, ref_col)

    return table
