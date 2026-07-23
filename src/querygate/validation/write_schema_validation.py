"""Write schema-truth validation (TODO.md item 93, Phase 1) — the write sibling
of `validation/schema_validation.py`. Reflects the single target table and
verifies every written column and every WHERE-clause column reference exists,
returning the reflected `sa.Table` for the write compiler.

Patch the module-level `_load_table` (re-exported from schema_validation) to
unit-test without a real database, exactly as for reads.
"""

from __future__ import annotations

from typing import Iterator, Optional

import sqlalchemy as sa

from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.query_ast.models import Predicate, WhereNode
from querygate.validation.schema_validation import (
    _load_table,
    parse_column_ref,
    predicate_column_refs,
    resolve_column,
)
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    WriteStatement,
)


def _where_predicates(node: WhereNode) -> Iterator[Predicate]:
    if isinstance(node, Predicate):
        yield node
        return
    if node.not_terms is not None:
        yield from _where_predicates(node.not_terms)
        return
    for child in node.and_terms or node.or_terms or []:
        yield from _where_predicates(child)


async def validate_write_schema(
    statement: WriteStatement, connection_id: str, principal: Optional[Principal] = None
) -> sa.Table:
    """Reflect the target table and verify every written column and WHERE ref
    exists. Returns the reflected table for the compiler."""
    table = await _load_table(connection_id, statement.table, connection_id)

    # Written columns exist (insert rows / update set).
    if isinstance(statement, InsertStatement):
        written = statement.rows[0].keys()
    elif isinstance(statement, UpdateStatement):
        written = statement.set.keys()
    else:
        written = []
    for column in written:
        resolve_column(table, column)  # raises QueryValidationError if missing

    # WHERE refs must exist AND must reference the single target table (a write
    # is single-table in Phase 1 — no correlated/other-table refs).
    where = getattr(statement, "where", None)
    if where is not None:
        for pred in _where_predicates(where):
            for ref in predicate_column_refs(pred):
                ref_table, ref_col = parse_column_ref(ref)
                if ref_table.lower() != statement.table.lower():
                    raise QueryValidationError(
                        f"A write's WHERE may only reference its target table "
                        f"{statement.table!r}, not {ref_table!r}"
                    )
                resolve_column(table, ref_col)

    return table
