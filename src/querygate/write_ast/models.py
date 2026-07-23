"""Pydantic AST for governed writes (TODO.md item 93, Phase 1).

The write sibling of `query_ast/models.py`: the ONLY shape in which a mutation
can be *proposed*. There is **no raw-DML field of any kind** — an `UPDATE`/
`DELETE` is a typed structure with a required `Predicate` filter tree (reused
verbatim from the read AST), and an `INSERT` is a list of typed rows. Every
statement is later checked against the live schema
(`validation/write_schema_validation.py`) and the active `WritePolicy`
(`validation/write_policy_validation.py`) before it is ever compiled — and in
Phase 1 it is only ever *previewed* (compiled, run in a transaction, diffed, and
rolled back), never committed.

Structural guarantees encoded here, not by lint:
- `UpdateStatement`/`DeleteStatement` **require** a `where` — an unqualified
  mutation cannot even be constructed.
- Single-table only in Phase 1 (no joins in a write target).
- Values are literal scalars in Phase 1 (scalar-function/CASE SET-values reuse
  the read surface in a later slice); no expression grammar is invented here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Union

import pydantic as pyd

from querygate.query_ast.models import WhereNode

WriteOp = Literal["insert", "update", "delete"]


class InsertStatement(pyd.BaseModel):
    """INSERT one or more typed rows into a single table. Each row is a mapping
    of bare column name -> literal scalar value (no expressions in Phase 1)."""

    op: Literal["insert"] = "insert"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    rows: List[Dict[str, Any]] = pyd.Field(
        min_length=1, description="Rows to insert; each maps column name -> literal value."
    )

    model_config = pyd.ConfigDict(extra="forbid")

    @pyd.model_validator(mode="after")
    def _validate_rows(self) -> "InsertStatement":
        if any(len(row) == 0 for row in self.rows):
            raise ValueError("Each insert row must set at least one column")
        # Every row must set the same column set — a ragged insert is almost
        # always a mistake and complicates the affected-columns policy check.
        first = set(self.rows[0])
        if any(set(row) != first for row in self.rows):
            raise ValueError("All insert rows must set the same set of columns")
        return self


class UpdateStatement(pyd.BaseModel):
    """UPDATE a single table's `set` columns for rows matching `where`. `where`
    is REQUIRED — an unqualified UPDATE cannot be expressed."""

    op: Literal["update"] = "update"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    set: Dict[str, Any] = pyd.Field(
        min_length=1, description="Column name -> new literal value. At least one."
    )
    where: WhereNode = pyd.Field(
        description="Required filter (a Predicate or WhereGroup) — no unqualified UPDATE."
    )

    model_config = pyd.ConfigDict(extra="forbid")


class DeleteStatement(pyd.BaseModel):
    """DELETE rows matching `where` from a single table. `where` is REQUIRED —
    an unqualified DELETE cannot be expressed."""

    op: Literal["delete"] = "delete"
    table: str = pyd.Field(description="Target table name (not Table.Column).")
    where: WhereNode = pyd.Field(
        description="Required filter (a Predicate or WhereGroup) — no unqualified DELETE."
    )

    model_config = pyd.ConfigDict(extra="forbid")


# Discriminated union dispatched on `op` (mirrors the read AST's registry-based
# select-item dispatch — no scattered isinstance branching at call sites).
WriteStatement = Union[InsertStatement, UpdateStatement, DeleteStatement]

_WRITE_STATEMENT_TYPES: Dict[str, type] = {
    "insert": InsertStatement,
    "update": UpdateStatement,
    "delete": DeleteStatement,
}


def write_target_table(statement: WriteStatement) -> str:
    """The single table a write statement targets."""
    return statement.table
