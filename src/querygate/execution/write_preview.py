"""Governed-writes dry-run preview (TODO.md item 93).

Answers "what would this write change?" **without ever mutating anything**. A
write is validated against the WritePolicy and live schema, compiled to a Core
DML statement (proving it is a real, bound-parameter statement with no raw SQL),
and its affected-row count is computed from a policy-checked `COUNT(*)` over the
same WHERE. **Nothing is ever committed.**

The scalar `WritePreview` fields are redaction-safe (operation, table, counts,
and *parameterized* SQL — never a value). With `include_diff` (phase 2b), the
preview also returns the bounded old→new **row diff** — the killer feature:
exactly which rows change and how. That diff necessarily carries values, so it
is deliberately bounded (`WritePolicy.max_diff_rows`) and masking-aware (a
read-masked column is redacted to `***MASKED***`), and it is a transient
response to the caller only — the audit event never carries these values. It is
computed by running the DML inside the same transaction and rolling it back, so
even the diff mutates nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pydantic as pyd
import sqlalchemy as sa

from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.compiler.write_compiler import _coerce_row, compile_write
from querygate.connections.engine import session_scope
from querygate.connections.visibility import resolve_visible_connection
from querygate.core.auth import Principal
from querygate.policy.loader import get_policy
from querygate.policy.models import Policy
from querygate.validation.write_policy_validation import validate_write_policy
from querygate.validation.write_schema_validation import validate_write_schema
from querygate.write_ast.models import (
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
    WriteStatement,
    to_read_where,
)

_MASKED = "***MASKED***"


class RowDiff(pyd.BaseModel):
    """One row's change in a dry-run diff. `before`/`after` are whole-row column
    maps; INSERT has no `before`, DELETE has no `after`. Masked columns are
    redacted to `***MASKED***`, so the diff never leaks a value read policy hides."""

    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None


class WriteDiff(pyd.BaseModel):
    """The bounded old→new preview of exactly what a write would change —
    computed by running the DML in a transaction and rolling it back. `truncated`
    is true when more rows are affected than `row_limit` shows."""

    rows: List[RowDiff]
    truncated: bool
    row_limit: int


class WritePreview(pyd.BaseModel):
    """Result of a dry-run write preview. The scalar fields are redaction-safe
    (counts + parameterized SQL, no values); `diff`, present only when explicitly
    requested, is the bounded, masking-aware old→new row change set."""

    operation: str
    table: str
    affected_rows: int
    within_affected_cap: bool
    max_affected_rows: int
    sql: str  # parameterized (bind placeholders), never literal values
    executed: bool = False  # ALWAYS false — a preview never commits
    diff: Optional[WriteDiff] = None

    model_config = pyd.ConfigDict(extra="forbid")


class WritePreviewService:
    """Thin service that validates + previews a write on one connection. Mirrors
    `StructuredQueryService` for the read path; there is no execute() sibling in
    Phase 1 by design."""

    def __init__(self, connection_id: str, principal: Optional[Principal] = None) -> None:
        self._connection_id = connection_id
        self._principal = principal

    async def preview(
        self, statement: WriteStatement, *, include_diff: bool = False
    ) -> WritePreview:
        policy = get_policy(self._connection_id, principal=self._principal)
        validate_write_policy(statement, policy, self._connection_id)
        table = await validate_write_schema(statement, self._connection_id, self._principal)

        # Compile the DML — proves it is a real, bound-parameter Core statement
        # with no raw SQL. Rendered to text with bind placeholders (never literals)
        # for the redaction-safe preview.
        profile, _ = resolve_visible_connection(self._connection_id, principal=self._principal)
        dml = compile_write(statement, table, profile.dialect)
        sql = str(dml.compile(compile_kwargs={"literal_binds": False}))

        max_rows = policy.write.max_affected_rows
        diff: Optional[WriteDiff] = None
        if isinstance(statement, (InsertStatement, UpsertStatement)):
            affected = len(statement.rows)
            # An upsert is insert-or-update per row; its old→new diff is a later
            # slice, so only a plain INSERT gets a diff preview here.
            if include_diff and isinstance(statement, InsertStatement):
                diff = self._insert_diff(statement, table, policy)
        else:
            # Affected-row count from a policy-checked COUNT(*) over the same
            # WHERE — a read, no DML, timeout-bounded by the session guardrails.
            count_stmt = (
                sa.select(sa.func.count())
                .select_from(table)
                .where(
                    _compile_where(
                        to_read_where(statement.where),
                        {statement.table: table},
                        {},
                        profile.dialect,
                    )
                )
            )
            async with session_scope(self._connection_id, policy=policy) as session:
                affected = int((await session.execute(count_stmt)).scalar_one())
                if include_diff:
                    # `within_cap` gates the DML-executing diff path (item 108):
                    # an over-cap write is rejected outright, so previewing it
                    # must never run a real row-locking UPDATE over every
                    # matching row just to build a diff the caller can't use.
                    diff = await self._mutation_diff(
                        session,
                        statement,
                        table,
                        dml,
                        policy,
                        profile.dialect,
                        within_cap=affected <= max_rows,
                    )
                # Defense-in-depth: never leave a transaction open that could
                # commit — the diff runs the DML, so this rollback is what makes
                # the whole preview non-mutating.
                await session.rollback()

        return WritePreview(
            operation=statement.op,
            table=table.name,
            affected_rows=affected,
            within_affected_cap=affected <= max_rows,
            max_affected_rows=max_rows,
            diff=diff,
            sql=sql,
            executed=False,
        )

    @staticmethod
    def _mask_row(row: Dict[str, Any], table_name: str, policy: Policy) -> Dict[str, Any]:
        """Redact any column the read policy masks, so a value hidden from a read
        can't leak through the write diff."""
        return {
            key: (_MASKED if policy.column_mask(table_name, key) is not None else value)
            for key, value in row.items()
        }

    def _insert_diff(
        self, statement: InsertStatement, table: sa.Table, policy: Policy
    ) -> WriteDiff:
        limit = policy.write.max_diff_rows
        shown = statement.rows[:limit]
        return WriteDiff(
            rows=[
                RowDiff(after=self._mask_row(_coerce_row(row, table), table.name, policy))
                for row in shown
            ],
            truncated=len(statement.rows) > limit,
            row_limit=limit,
        )

    async def _mutation_diff(
        self,
        session,
        statement: WriteStatement,
        table: sa.Table,
        dml,
        policy: Policy,
        dialect: str,
        *,
        within_cap: bool = True,
    ) -> WriteDiff:
        """Bounded old→new diff for an UPDATE/DELETE, computed inside the
        (about-to-be-rolled-back) transaction. DELETE shows the rows that would
        disappear (a bounded SELECT — it never runs the DML); UPDATE runs the DML
        and re-reads the affected rows by single-column primary key to show the
        real committed-shape old→new, falling back to applying the SET in Python
        for a composite/absent PK.

        `within_cap=False` (the affected count already exceeds
        `WritePolicy.max_affected_rows`) forces that same Python fallback for
        UPDATE — TODO.md item 108. Otherwise `include_diff=true` against a broad
        WHERE would run a real row-locking UPDATE over *every* matching row —
        taking locks, generating WAL/redo and contending with live writers — to
        preview a write that is rejected outright as over-cap. The caller still
        gets a useful bounded diff; only the DML is skipped. The Python fallback
        is an approximation (it can't reflect DB-side defaults/triggers/coercion),
        which is the right trade for a write that will not be allowed to run."""
        limit = policy.write.max_diff_rows
        where = _compile_where(
            to_read_where(statement.where), {statement.table: table}, {}, dialect
        )
        before_stmt = sa.select(table).where(where).limit(limit + 1)
        before_rows = [dict(r) for r in (await session.execute(before_stmt)).mappings().all()]
        truncated = len(before_rows) > limit
        before_rows = before_rows[:limit]

        if not isinstance(statement, UpdateStatement):  # DELETE — rows are removed
            return WriteDiff(
                rows=[RowDiff(before=self._mask_row(r, table.name, policy)) for r in before_rows],
                truncated=truncated,
                row_limit=limit,
            )

        pk_cols = list(table.primary_key.columns)
        if within_cap and len(pk_cols) == 1 and before_rows:
            pk = pk_cols[0]
            keys = [r[pk.name] for r in before_rows]
            await session.execute(dml)  # applied in-txn, rolled back by the caller
            after_by_key = {
                r[pk.name]: dict(r)
                for r in (await session.execute(sa.select(table).where(pk.in_(keys)))).mappings()
            }
            rows = [
                RowDiff(
                    before=self._mask_row(b, table.name, policy),
                    after=self._mask_row(after_by_key.get(b[pk.name], {}), table.name, policy),
                )
                for b in before_rows
            ]
        else:
            set_values = _coerce_row(statement.set, table)
            rows = [
                RowDiff(
                    before=self._mask_row(b, table.name, policy),
                    after=self._mask_row({**b, **set_values}, table.name, policy),
                )
                for b in before_rows
            ]
        return WriteDiff(rows=rows, truncated=truncated, row_limit=limit)
