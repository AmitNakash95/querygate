"""Governed-writes dry-run preview (TODO.md item 93, Phase 1).

Answers "what would this write change?" **without ever mutating anything** — the
core Phase-1 surface. A write is validated against the WritePolicy and live
schema, compiled to a Core DML statement (proving it is a real, bound-parameter
statement with no raw SQL), and its affected-row count is computed from a
policy-checked `COUNT(*)` over the same WHERE. **No DML is executed and nothing
is committed** — execution is a later, separately-gated phase.

The returned `WritePreview` is redaction-safe: it carries the operation, target
table, affected-row count and whether that is within the policy cap, and the
*parameterized* SQL (bind placeholders, never literal values). It never contains
a row value, a SET value, or a predicate literal.
"""

from __future__ import annotations

from typing import List, Optional

import pydantic as pyd
import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.compiler.write_compiler import compile_write
from querygate.connections.engine import session_scope
from querygate.core.auth import Principal
from querygate.core.exceptions import QueryValidationError
from querygate.policy.loader import get_policy
from querygate.query_ast.models import Predicate, WhereNode
from querygate.validation.write_policy_validation import validate_write_policy
from querygate.validation.write_schema_validation import validate_write_schema
from querygate.write_ast.models import InsertStatement, WriteStatement


class WritePreview(pyd.BaseModel):
    """Redaction-safe result of a dry-run write preview. No row/predicate values."""

    operation: str
    table: str
    affected_rows: int
    within_affected_cap: bool
    max_affected_rows: int
    sql: str  # parameterized (bind placeholders), never literal values
    executed: bool = False  # ALWAYS false in Phase 1 — nothing is ever committed

    model_config = pyd.ConfigDict(extra="forbid")


def _reject_subquery_in_write_where(where: Optional[WhereNode]) -> None:
    """A value_subquery (item 97) inside a write WHERE is out of scope for write
    Phase 1 — reject it rather than silently mis-handle it."""
    if where is None:
        return
    stack: List[WhereNode] = [where]
    while stack:
        node = stack.pop()
        if isinstance(node, Predicate):
            if node.value_subquery is not None:
                raise QueryValidationError(
                    "IN (subquery) is not supported in a write's WHERE (item 93 phase 1)"
                )
            continue
        if node.not_terms is not None:
            stack.append(node.not_terms)
        stack.extend(node.and_terms or node.or_terms or [])


class WritePreviewService:
    """Thin service that validates + previews a write on one connection. Mirrors
    `StructuredQueryService` for the read path; there is no execute() sibling in
    Phase 1 by design."""

    def __init__(self, connection_id: str, principal: Optional[Principal] = None) -> None:
        self._connection_id = connection_id
        self._principal = principal

    async def preview(self, statement: WriteStatement) -> WritePreview:
        policy = get_policy(self._connection_id, principal=self._principal)
        validate_write_policy(statement, policy, self._connection_id)
        _reject_subquery_in_write_where(getattr(statement, "where", None))
        table = await validate_write_schema(statement, self._connection_id, self._principal)

        # Compile the DML — proves it is a real, bound-parameter Core statement
        # with no raw SQL. Rendered to text with bind placeholders (never literals)
        # for the redaction-safe preview.
        dml = compile_write(statement, table)
        sql = str(dml.compile(compile_kwargs={"literal_binds": False}))

        max_rows = policy.write.max_affected_rows
        if isinstance(statement, InsertStatement):
            affected = len(statement.rows)
        else:
            # Affected-row count from a policy-checked COUNT(*) over the same
            # WHERE — a read, no DML, timeout-bounded by the session guardrails.
            count_stmt = (
                sa.select(sa.func.count())
                .select_from(table)
                .where(_compile_where(statement.where, {statement.table: table}, alias_map={}))
            )
            async with session_scope(self._connection_id, policy=policy) as session:
                result = await session.execute(count_stmt)
                affected = int(result.scalar_one())
                # Defense-in-depth: never leave a transaction open that could commit.
                await session.rollback()

        return WritePreview(
            operation=statement.op,
            table=table.name,
            affected_rows=affected,
            within_affected_cap=affected <= max_rows,
            max_affected_rows=max_rows,
            sql=sql,
            executed=False,
        )
