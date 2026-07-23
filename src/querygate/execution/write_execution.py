"""Governed-writes gated execution (TODO.md item 93, Phase 2a).

The execute() sibling of `WritePreviewService`: it actually commits a
single-table INSERT/UPDATE/DELETE — but only through the *same* validated
`write_ast` -> `compile_write` Core statement the preview compiles, so there is
no raw-DML path, and only when the write is fully in policy, within its
affected-row cap, and (if the policy asks) human-approved.

The safety model (all enforced here, all tested):

- **Deny-by-default** — `validate_write_policy` rejects unless the WritePolicy
  is enabled and the table/operation are allowed.
- **No raw DML** — execution runs the compiled bound-parameter Core statement;
  an injected value can only ever populate a placeholder.
- **In-transaction row cap** — the affected rows are counted inside the same
  transaction that will mutate them, and the write aborts (rolls back) *before*
  committing if the count exceeds `max_affected_rows`; the executed statement's
  own rowcount is re-checked against the cap before commit, so a concurrent
  insert cannot push the write over its cap.
- **One transaction, no partial write** — the whole thing runs in the single
  transaction `session_scope` opens; any error rolls it all back.
- **Approval gate** — a write over `require_approval_over_rows` pauses with
  `ApprovalRequiredError` (item 92 machinery, fingerprint-bound token) unless a
  valid approval token is supplied.
- **Redaction-safe, dual-identity, tamper-evident audit** — reuses `audit_query`
  (operation `execute_structured_write`), so per-human attribution (item 90) and
  the hash-chained ledger (item 91) apply; only the op, table, affected count,
  and parameterized SQL are recorded — never a value or row.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, List, Optional

import pydantic as pyd
import sqlalchemy as sa
from sqlalchemy.exc import DataError, IntegrityError, StatementError

from querygate.audit.events import AuditSurface
from querygate.audit.logger import audit_query
from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.compiler.write_compiler import _coerce_write_value, compile_write
from querygate.connections.engine import session_scope
from querygate.core.auth import Principal
from querygate.core.config import config as app_config
from querygate.core.exceptions import (
    ApprovalRequiredError,
    QueryValidationError,
    public_error_message,
)
from querygate.execution.approval import verify_approval_token, write_fingerprint
from querygate.execution.compensation import (
    CompensationRecord,
    compensation_expiry,
    get_compensation_store,
    new_compensation_id,
)
from querygate.execution.concurrency import concurrency_slot
from querygate.execution.write_preview import _reject_subquery_in_write_where
from querygate.policy.loader import get_policy
from querygate.policy.models import WritePolicy
from querygate.query_ast.models import Predicate
from querygate.validation.write_policy_validation import validate_write_policy
from querygate.validation.write_schema_validation import validate_write_schema
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    WriteStatement,
)

if TYPE_CHECKING:
    from querygate.execution.service import ApprovalResolver


class WriteResult(pyd.BaseModel):
    """Redaction-safe result of a committed governed write. No row/predicate
    values — the op, target table, and how many rows were affected."""

    operation: str
    table: str
    affected_rows: int
    executed: bool = True
    # Set when the policy enabled bounded reversibility and this write was
    # captured (item 93 phase 3a): pass it to POST /write/undo to reverse it.
    # `null` means this write is NOT undoable — compensation is off, the affected
    # set exceeded max_compensation_rows, the table has no single-column PK, or an
    # INSERT used a server-generated PK.
    compensation_id: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


class WriteBatchItemResult(pyd.BaseModel):
    """One write's outcome in a batch — a committed `WriteResult`'s fields, or an
    `error` (a failing write never drops the rest of the batch)."""

    operation: Optional[str] = None
    table: Optional[str] = None
    affected_rows: Optional[int] = None
    executed: bool = False
    compensation_id: Optional[str] = None
    error: Optional[str] = None


def _write_shape(statement: WriteStatement, table_name: str) -> dict:
    """A redaction-safe shape of the write for the audit event: op, table, and
    the *names* of written columns — never a value."""
    if isinstance(statement, InsertStatement):
        columns = sorted(statement.rows[0].keys()) if statement.rows else []
    else:
        # UpdateStatement.set is column->value; DeleteStatement writes no columns.
        columns = sorted(getattr(statement, "set", {}).keys())
    return {"op": statement.op, "table": table_name, "columns": columns}


class WriteExecutionService:
    """Validate, compile, and *commit* one structured write on `connection_id`.

    Deliberately a separate class from `WritePreviewService` (preview never
    commits) and `StructuredQueryService` (reads), but it feeds the same
    validate -> policy -> schema -> compile -> execute -> audit spine.
    """

    def __init__(
        self,
        connection_id: str,
        principal: Optional[Principal] = None,
        surface: AuditSurface = "internal",
    ) -> None:
        self._connection_id = connection_id
        self._principal = principal
        self._surface = surface

    async def execute(
        self, statement: WriteStatement, *, approval_token: Optional[str] = None
    ) -> WriteResult:
        start = time.monotonic()
        policy = get_policy(self._connection_id, principal=self._principal)
        sql = ""
        table: Optional[sa.Table] = None
        try:
            # Policy + schema validation run first, before any DB touch — exactly
            # as the preview path does, so an execute can never pass a check a
            # preview would fail. Deny-by-default lives inside validate_write_policy.
            # Kept inside the try so a *rejected* write attempt (denied table,
            # over-cap, unqualified) is audited too — the same as the read path.
            validate_write_policy(statement, policy, self._connection_id)
            _reject_subquery_in_write_where(getattr(statement, "where", None))
            table = await validate_write_schema(statement, self._connection_id, self._principal)
            dml = compile_write(statement, table)
            sql = str(dml.compile(compile_kwargs={"literal_binds": False}))

            # Writes share the connection's concurrency slot for now; a dedicated
            # write limiter (so writes can't starve or be starved by reads) is a
            # phase-2b/3 item, tracked in TODO.md item 93.
            async with concurrency_slot(
                self._connection_id,
                policy.max_concurrency,
                policy.concurrency_wait_seconds,
                principal_subject=self._principal_subject,
                max_queue_depth=policy.max_queue_depth,
                max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
            ):
                affected, compensation_id = await self._execute_in_transaction(
                    statement, table, dml, policy, approval_token
                )
        except Exception as exc:
            self._audit(sql, statement, table, affected_rows=None, rejected=True, error=exc)
            raise

        self._audit(
            sql,
            statement,
            table,
            affected_rows=affected,
            rejected=False,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return WriteResult(
            operation=statement.op,
            table=table.name,
            affected_rows=affected,
            compensation_id=compensation_id,
        )

    async def undo(self, compensation_id: str) -> WriteResult:
        """Reverse a previously-committed write (item 93 phase 3a). The record
        must exist, be unexpired, unconsumed, and belong to this connection;
        otherwise a clean rejection. All inverse statements run in **one
        transaction** — undo is atomic all-or-nothing, upholding the same
        "no partial write" guarantee as the forward path (it never leaves a
        half-reversed change). Consumed only on success so a failed undo can be
        retried and a successful one can't be replayed.

        Authorization: possession of the (single-use, TTL'd, connection-scoped)
        `compensation_id` — only ever returned to whoever executed the original
        governed write. Undo therefore does NOT re-trigger the approval gate (the
        forward write was already approved and undo restores the *prior* state,
        which is lower-risk) and does NOT re-check the inverse op against
        `allowed_operations` (undoing a DELETE is an INSERT — requiring INSERT to
        be separately enabled would make reversibility unusable). It still
        enforces deny-by-default (writes must be enabled + the table writable),
        the affected-row cap, schema truth, and full audit — see the Decision Log.
        """
        record = await get_compensation_store().get(compensation_id)
        if record is None or record.connection_id != self._connection_id:
            raise QueryValidationError(
                "unknown, expired, already-used, or wrong-connection compensation id"
            )
        try:
            total = await self._apply_undo_atomically(record)
        except Exception as exc:
            self._audit_undo(record, affected_rows=None, rejected=True, error=exc)
            raise
        await get_compensation_store().consume(compensation_id)
        self._audit_undo(record, affected_rows=total, rejected=False)
        return WriteResult(operation=f"undo_{record.op}", table=record.table, affected_rows=total)

    def _build_undo(self, record: CompensationRecord) -> List[WriteStatement]:
        """The inverse governed write(s): re-INSERT deleted rows, DELETE inserted
        keys, or restore each updated row's changed columns by primary key."""
        pk_ref = f"{record.table}.{record.pk_column}"
        if record.op == "delete":
            return [InsertStatement(table=record.table, rows=record.pre_image)]
        if record.op == "insert":
            return [
                DeleteStatement(
                    table=record.table,
                    where=Predicate(col=pk_ref, op="in", value=record.inserted_keys),
                )
            ]
        # UPDATE undo: one restore per row (each row's old values differ), keyed
        # on its PK, restoring ONLY the columns the original write changed — so a
        # concurrent change to a column this write never touched is preserved.
        statements: List[WriteStatement] = []
        for row in record.pre_image:
            restore = {c: row[c] for c in record.changed_columns if c in row}
            if not restore:
                continue
            statements.append(
                UpdateStatement(
                    table=record.table,
                    set=restore,
                    where=Predicate(col=pk_ref, op="eq", value=row[record.pk_column]),
                )
            )
        return statements

    async def _apply_undo_atomically(self, record: CompensationRecord) -> int:
        """Apply every inverse statement in ONE transaction, committing once — any
        failure rolls the whole undo back (atomic). Deny-by-default and the
        affected-row cap still hold; the approval and op-allowed gates are
        deliberately bypassed (see `undo`). Does not capture new compensation, so
        an undo never spawns orphaned redo records. For an UPDATE, refuses if a
        changed row drifted from the write's post-image (optimistic concurrency)."""
        statements = self._build_undo(record)
        policy = get_policy(self._connection_id, principal=self._principal)
        if not policy.write.table_writable(record.table):
            raise QueryValidationError(
                f"writes are not enabled for table {record.table!r}; cannot undo"
            )
        cap = policy.write.max_affected_rows
        total = 0
        async with concurrency_slot(
            self._connection_id,
            policy.max_concurrency,
            policy.concurrency_wait_seconds,
            principal_subject=self._principal_subject,
            max_queue_depth=policy.max_queue_depth,
            max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
        ):
            async with session_scope(self._connection_id, policy=policy) as session:
                if record.op == "update" and record.post_values:
                    await self._assert_no_update_drift(session, record)
                for stmt in statements:
                    table = await validate_write_schema(stmt, self._connection_id, self._principal)
                    dml = compile_write(stmt, table)
                    affected = await self._count_affected(session, stmt, table)
                    if affected > cap:
                        raise QueryValidationError(
                            f"undo would affect {affected} rows, over the "
                            f"max_affected_rows cap of {cap}"
                        )
                    try:
                        await session.execute(dml)
                    except (IntegrityError, DataError, StatementError) as exc:
                        raise QueryValidationError(
                            "undo violates a database constraint or value type and was "
                            "rolled back — nothing was committed"
                        ) from exc
                    total += len(stmt.rows) if isinstance(stmt, InsertStatement) else affected
                await session.commit()
        return total

    async def _assert_no_update_drift(self, session, record: CompensationRecord) -> None:
        """Optimistic concurrency for UPDATE undo: read each affected row's current
        changed-column values and refuse the undo if any differs from what the
        write set (or the row is gone) — so a concurrent change since the write is
        never silently overwritten."""
        table = await validate_write_schema(
            UpdateStatement(
                table=record.table,
                set={c: record.post_values[c] for c in record.changed_columns},
                where=Predicate(
                    col=f"{record.table}.{record.pk_column}",
                    op="eq",
                    value=record.pre_image[0][record.pk_column],
                ),
            ),
            self._connection_id,
            self._principal,
        )
        pk = record.pk_column
        keys = [row[pk] for row in record.pre_image]
        cols = [table.c[pk]] + [table.c[c] for c in record.changed_columns if c in table.c]
        rows = (await session.execute(sa.select(*cols).where(table.c[pk].in_(keys)))).mappings()
        current = {row[pk]: dict(row) for row in rows}
        for snapshot in record.pre_image:
            cur = current.get(snapshot[pk])
            if cur is None:
                raise QueryValidationError(
                    "a row changed since the write (it no longer exists); undo refused "
                    "to avoid clobbering a concurrent change"
                )
            for col in record.changed_columns:
                expected = _coerce_write_value(table.c[col], record.post_values[col])
                if cur[col] != expected:
                    raise QueryValidationError(
                        "a row changed since the write; undo refused to avoid clobbering "
                        "a concurrent change (optimistic concurrency)"
                    )

    async def execute_many(
        self,
        statements: List[WriteStatement],
        *,
        approval_resolver: Optional["ApprovalResolver"] = None,
    ) -> List[WriteBatchItemResult]:
        """Run each write independently — one failure (or a rejected approval)
        surfaces as that item's `error` without dropping the rest. Each write is
        its **own** transaction (a batch is not one atomic multi-statement
        transaction — that stronger semantic is a later phase). `approval_resolver`
        is the interactive-approval seam (MCP elicitation): when a write trips the
        gate and no token covers it, the resolver gets one chance to obtain one,
        then the write is retried once."""
        return [await self._execute_batch_item(s, approval_resolver) for s in statements]

    async def _execute_batch_item(
        self, statement: WriteStatement, approval_resolver: Optional["ApprovalResolver"]
    ) -> WriteBatchItemResult:
        try:
            return self._batch_ok(await self.execute(statement))
        except ApprovalRequiredError as exc:
            if approval_resolver is not None:
                token = await approval_resolver(statement, exc)
                if token is not None:
                    try:
                        return self._batch_ok(await self.execute(statement, approval_token=token))
                    except Exception as retry_exc:  # shaped into the item error below
                        exc = retry_exc  # type: ignore[assignment]
            return self._batch_error(statement, exc)
        except Exception as exc:
            return self._batch_error(statement, exc)

    @staticmethod
    def _batch_ok(result: WriteResult) -> WriteBatchItemResult:
        return WriteBatchItemResult(
            operation=result.operation,
            table=result.table,
            affected_rows=result.affected_rows,
            executed=True,
            compensation_id=result.compensation_id,
        )

    @staticmethod
    def _batch_error(statement: WriteStatement, exc: Exception) -> WriteBatchItemResult:
        return WriteBatchItemResult(
            operation=statement.op, table=statement.table, error=public_error_message(exc)
        )

    async def _execute_in_transaction(
        self,
        statement: WriteStatement,
        table: sa.Table,
        dml,
        policy,
        approval_token: Optional[str],
    ) -> tuple[int, Optional[str]]:
        """One transaction: count -> cap -> approval -> capture pre-image ->
        mutate -> re-check cap -> commit. Any raise propagates to `session_scope`,
        which rolls back — so there is never a partial or over-cap committed
        write. Returns (affected_rows, compensation_id-or-None)."""
        cap = policy.write.max_affected_rows
        write_policy = policy.write
        async with session_scope(self._connection_id, policy=policy) as session:
            # Count the rows this write will affect, in the same transaction that
            # will mutate them, before mutating.
            affected = await self._count_affected(session, statement, table)
            if affected > cap:
                raise QueryValidationError(
                    f"write would affect {affected} rows, over the max_affected_rows "
                    f"cap of {cap}"
                )
            # Human-in-the-loop approval on the write, before it mutates anything.
            self._enforce_write_approval_gate(statement, affected, write_policy, approval_token)

            # Bounded reversibility (item 93 phase 3a): capture the pre-image in
            # the same transaction, before mutating — held in memory and only
            # committed to the compensation store after the write commits.
            pending_compensation = await self._capture_pre_image(
                session, statement, table, affected, write_policy
            )
            # A server-generated PK isn't known until after the INSERT runs, so
            # capture it via RETURNING to make serial/identity-PK inserts undoable
            # too (item 93 phase 3b — otherwise compensation_id would be null).
            returning_pk = self._insert_returning_pk(statement, table, affected, write_policy)
            exec_dml = dml.returning(returning_pk) if returning_pk is not None else dml

            try:
                result = await session.execute(exec_dml)
            except (IntegrityError, DataError, StatementError) as exc:
                # A constraint (NOT NULL / FK / unique), a bad value type, or a
                # bind error is the caller's fault, not a server fault — turn the
                # opaque driver error into a clean, typed 4xx and roll back (the
                # raise propagates to session_scope). The safe message names the
                # *class* of problem without echoing raw driver/schema text, so
                # neither the response nor the audit leaks internals.
                raise QueryValidationError(
                    "the write violates a database constraint or value type "
                    "(not-null, foreign key, unique, or a mistyped value) and was "
                    "rolled back — nothing was committed"
                ) from exc
            if returning_pk is not None:
                pending_compensation = CompensationRecord(
                    compensation_id=new_compensation_id(),
                    connection_id=self._connection_id,
                    table=table.name,
                    op="insert",
                    pk_column=returning_pk.name,
                    inserted_keys=list(result.scalars().all()),
                    expires_at=compensation_expiry(write_policy.compensation_ttl_seconds),
                )
            # Belt-and-suspenders: the statement's own rowcount must also be within
            # cap, so a race between the count and the mutation can't over-write.
            actual = (
                len(statement.rows)
                if isinstance(statement, InsertStatement)
                else (
                    result.rowcount
                    if result.rowcount is not None and result.rowcount >= 0
                    else affected
                )
            )
            if actual > cap:
                raise QueryValidationError(
                    f"write affected {actual} rows, over the max_affected_rows cap of {cap}"
                )
            await session.commit()

        # The write is durably committed — only now record the compensation, so a
        # rolled-back write never leaves an undo pointing at a change that didn't
        # happen.
        compensation_id = None
        if pending_compensation is not None:
            await get_compensation_store().put(pending_compensation)
            compensation_id = pending_compensation.compensation_id
        return actual, compensation_id

    @staticmethod
    def _insert_returning_pk(
        statement: WriteStatement, table: sa.Table, affected: int, write_policy
    ) -> Optional[sa.Column]:
        """The single PK column to capture via RETURNING, for an INSERT with a
        server-generated key (so it's undoable), or None. Only when compensation
        is on, within the snapshot cap, the table has one PK column, and at least
        one row omits it (a supplied key needs no RETURNING — `_capture_pre_image`
        records it directly)."""
        if not write_policy.compensation_enabled or affected > write_policy.max_compensation_rows:
            return None
        if not isinstance(statement, InsertStatement):
            return None
        pk_cols = list(table.primary_key.columns)
        if len(pk_cols) != 1:
            return None
        pk = pk_cols[0]
        if any(row.get(pk.name) is None for row in statement.rows):
            return pk
        return None

    async def _capture_pre_image(
        self, session, statement: WriteStatement, table: sa.Table, affected: int, write_policy
    ) -> Optional[CompensationRecord]:
        """Build (but do not yet store) the compensation record for this write, or
        None if compensation is off, the affected set is over the snapshot cap, or
        the table lacks the single-column primary key an undo needs to key on."""
        if not write_policy.compensation_enabled or affected > write_policy.max_compensation_rows:
            return None
        pk_cols = list(table.primary_key.columns)
        if len(pk_cols) != 1:
            return None  # undo keys on a single-column PK (documented limit)
        pk = pk_cols[0].name

        record = CompensationRecord(
            compensation_id=new_compensation_id(),
            connection_id=self._connection_id,
            table=table.name,
            op=statement.op,
            pk_column=pk,
            expires_at=compensation_expiry(write_policy.compensation_ttl_seconds),
        )
        where = {statement.table: table}
        if isinstance(statement, InsertStatement):
            keys = [row.get(pk) for row in statement.rows]
            if any(k is None for k in keys):
                # A server-generated PK (serial/identity) isn't known here without
                # RETURNING, so this INSERT is NOT undoable: the caller sees this
                # as compensation_id=None (documented on WritePolicy / the
                # endpoint). Supply the PK to make an INSERT undoable; RETURNING
                # capture is a phase-3b follow-up.
                return None
            record.inserted_keys = keys
        elif isinstance(statement, UpdateStatement):
            # Snapshot ONLY the primary key + the columns this UPDATE changes, so
            # undo restores exactly what was changed and nothing else.
            record.changed_columns = [c for c in statement.set.keys() if c != pk]
            # The values the write sets — undo refuses if the row drifted from these.
            record.post_values = {c: statement.set[c] for c in record.changed_columns}
            cols = [table.c[pk]] + [table.c[c] for c in record.changed_columns if c in table.c]
            pre_stmt = (
                sa.select(*cols)
                .where(_compile_where(statement.where, where, alias_map={}))
                .limit(write_policy.max_compensation_rows)
            )
            record.pre_image = [dict(r) for r in (await session.execute(pre_stmt)).mappings().all()]
        else:  # DELETE: snapshot the full rows so the undo can re-insert them.
            pre_stmt = (
                sa.select(table)
                .where(_compile_where(statement.where, where, alias_map={}))
                .limit(write_policy.max_compensation_rows)
            )
            record.pre_image = [dict(r) for r in (await session.execute(pre_stmt)).mappings().all()]
        return record

    async def _count_affected(self, session, statement: WriteStatement, table: sa.Table) -> int:
        if isinstance(statement, InsertStatement):
            return len(statement.rows)
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(table)
            .where(_compile_where(statement.where, {statement.table: table}, alias_map={}))
        )
        result = await session.execute(count_stmt)
        return int(result.scalar_one())

    def _enforce_write_approval_gate(
        self,
        statement: WriteStatement,
        affected: int,
        write_policy: WritePolicy,
        approval_token: Optional[str],
    ) -> None:
        threshold = write_policy.require_approval_over_rows
        if threshold is None or affected <= threshold:
            return
        fingerprint = write_fingerprint(statement)
        if approval_token and verify_approval_token(
            approval_token, fingerprint=fingerprint, key=app_config.approval_token_hmac_key
        ):
            return
        raise ApprovalRequiredError(
            f"write affects {affected} rows; human approval is required before it runs",
            fingerprint=fingerprint,
            reasons=[
                f"write affects {affected} rows (over approval threshold {threshold})",
            ],
        )

    # ---- audit / principal decomposition (mirrors StructuredQueryService) ---- #

    def _audit(
        self,
        sql: str,
        statement: WriteStatement,
        table: Optional[sa.Table],
        *,
        affected_rows: Optional[int],
        rejected: bool,
        error: Optional[Exception] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        # `table` is None when validation rejected the write before reflection —
        # fall back to the statement's own (unverified) target name for the shape.
        table_name = table.name if table is not None else statement.table
        audit_query(
            connection_id=self._connection_id,
            sql=sql,
            row_count=affected_rows,
            duration_ms=duration_ms,
            principal=self._principal_subject,
            principal_scopes=self._principal_scopes,
            actor=self._principal_actor,
            delegation_chain=self._delegation_chain,
            auth_method=self._auth_method,
            surface=self._surface,
            operation="execute_structured_write",
            query_shape=_write_shape(statement, table_name),
            policy_decision="denied" if rejected else "allowed",
            rejected=rejected,
            rejection_reason=(f"{type(error).__name__}: {error}" if error else None),
            error_category=(type(error).__name__ if error else None),
        )

    def _audit_undo(
        self,
        record: CompensationRecord,
        *,
        affected_rows: Optional[int],
        rejected: bool,
        error: Optional[Exception] = None,
    ) -> None:
        """Audit an undo as a single redaction-safe `undo_structured_write` event
        — op/table/affected-count only, never a restored value."""
        audit_query(
            connection_id=self._connection_id,
            sql="",
            row_count=affected_rows,
            principal=self._principal_subject,
            principal_scopes=self._principal_scopes,
            actor=self._principal_actor,
            delegation_chain=self._delegation_chain,
            auth_method=self._auth_method,
            surface=self._surface,
            operation="undo_structured_write",
            query_shape={"op": f"undo_{record.op}", "table": record.table},
            policy_decision="denied" if rejected else "allowed",
            rejected=rejected,
            rejection_reason=(f"{type(error).__name__}: {error}" if error else None),
            error_category=(type(error).__name__ if error else None),
        )

    @property
    def _principal_subject(self) -> Optional[str]:
        return self._principal.subject if self._principal else None

    @property
    def _principal_scopes(self) -> Optional[List[str]]:
        return sorted(self._principal.scopes) if self._principal else None

    @property
    def _auth_method(self) -> str:
        return self._principal.auth_method if self._principal else "unknown"

    @property
    def _principal_actor(self) -> Optional[str]:
        return self._principal.actor_subject if self._principal else None

    @property
    def _delegation_chain(self) -> Optional[List[str]]:
        return self._principal.delegation_chain if self._principal else None
