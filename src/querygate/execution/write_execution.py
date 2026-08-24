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
from typing import TYPE_CHECKING, Dict, List, Optional

import pydantic as pyd
import sqlalchemy as sa
from sqlalchemy.exc import DataError, IntegrityError, StatementError

from querygate.audit.events import AuditSurface
from querygate.audit.logger import audit_query
from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.compiler.write_compiler import compile_write
from querygate.connections.engine import session_scope
from querygate.connections.visibility import resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.config import config as app_config
from querygate.core.exceptions import (
    ApprovalRequiredError,
    QueryValidationError,
    public_error_message,
)
from querygate.execution.approval import TOKEN_KIND_GRANT, verify_approval_token, write_fingerprint
from querygate.execution.concurrency import concurrency_slot
from querygate.policy.loader import get_policy
from querygate.policy.models import WritePolicy
from querygate.validation.write_policy_validation import (
    validate_write_batch_size,
    validate_write_policy,
)
from querygate.validation.write_schema_validation import validate_write_schema
from querygate.write_ast.models import (
    InsertStatement,
    UpsertStatement,
    WriteStatement,
    to_read_where,
)

if TYPE_CHECKING:
    from querygate.execution.service import ApprovalResolver


# Response models live in `results.py` so this module defines no
# `pydantic.BaseModel` subclass and can therefore be Cython-compiled — this is
# item 211's write-side gate site. Re-exported so existing
# `from querygate.execution.write_execution import WriteResult` imports keep working.
from querygate.execution.results import (  # noqa: E402
    WriteBatchItemResult,
    WriteResult,
)


def _write_shape(statement: WriteStatement, table_name: str) -> dict:
    """A redaction-safe shape of the write for the audit event: op, table, and
    the *names* of written columns — never a value."""
    if isinstance(statement, (InsertStatement, UpsertStatement)):
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

    def _connection_dialect(self) -> str:
        """The target connection's dialect — selects the upsert idiom (and is
        harmlessly ignored for dialect-agnostic insert/update/delete)."""
        profile, _policy = resolve_visible_connection(
            self._connection_id, principal=self._principal
        )
        return profile.dialect

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
            table = await validate_write_schema(statement, self._connection_id, self._principal)
            dml = compile_write(statement, table, self._connection_dialect())
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
                affected = await self._execute_in_transaction(
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
        )

    async def execute_many(
        self,
        statements: List[WriteStatement],
        *,
        approval_tokens: Optional[Dict[str, str]] = None,
        approval_resolver: Optional["ApprovalResolver"] = None,
        atomic: bool = False,
    ) -> List[WriteBatchItemResult]:
        """Run a batch of writes. Default (`atomic=False`): each write is its own
        transaction — one failure (or a rejected approval) surfaces as that item's
        `error` without dropping the rest. `approval_tokens` maps a write
        fingerprint to its pre-obtained approval token (TODO.md item 128's MCP
        MRTR port — the write-side sibling of `StructuredQueryService.execute_many`'s
        identical parameter), tried before any resolver; `approval_resolver` (an
        older, still-supported in-process interactive path) gets one chance to
        obtain a token when no pre-supplied one covers a gated write. `atomic=True`:
        **all-or-nothing** — every write runs in ONE transaction and any failure
        rolls the whole batch back (every item then reports the same error). An
        atomic batch is deny-by-default + capped like any write and fails closed on
        an approval-gated write (there is no per-item token channel in atomic
        mode)."""
        # Bound the batch at the service layer too, not only at the MCP
        # transport (item 109) — so any future batch caller is capped by
        # construction rather than by remembering to check.
        validate_write_batch_size(
            len(statements), get_policy(self._connection_id, principal=self._principal)
        )
        if atomic:
            return await self._execute_many_atomically(statements)
        return [
            await self._execute_batch_item(s, approval_tokens, approval_resolver)
            for s in statements
        ]

    async def _execute_many_atomically(
        self, statements: List[WriteStatement]
    ) -> List[WriteBatchItemResult]:
        policy = get_policy(self._connection_id, principal=self._principal)
        dialect = self._connection_dialect()
        cap = policy.write.max_affected_rows
        oks: List[WriteBatchItemResult] = []
        # Policy for EVERY statement up front, before a slot is taken or a session
        # opened (item 116): validation is pure CPU, and checking it inside the loop
        # meant statement N's caps were only applied after 1..N-1 had already
        # compiled and issued DML — rolled back, but the work was performed, which is
        # the very thing the shape caps exist to prevent. Mirrors item 109's
        # up-front batch-size check. Schema validation stays in the loop: it needs
        # the connection.
        for statement in statements:
            validate_write_policy(statement, policy, self._connection_id)
        try:
            async with concurrency_slot(
                self._connection_id,
                policy.max_concurrency,
                policy.concurrency_wait_seconds,
                principal_subject=self._principal_subject,
                max_queue_depth=policy.max_queue_depth,
                max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
            ):
                async with session_scope(self._connection_id, policy=policy) as session:
                    for stmt in statements:
                        validate_write_policy(stmt, policy, self._connection_id)
                        table = await validate_write_schema(
                            stmt, self._connection_id, self._principal
                        )
                        affected = await self._count_affected(session, stmt, table)
                        if affected > cap:
                            raise QueryValidationError(
                                f"a write in the atomic batch would affect {affected} rows, "
                                f"over the max_affected_rows cap of {cap}"
                            )
                        # Fail closed on an approval-gated write (no per-item token
                        # channel in atomic mode).
                        self._enforce_write_approval_gate(stmt, affected, policy.write, None)
                        try:
                            await session.execute(compile_write(stmt, table, dialect))
                        except (IntegrityError, DataError, StatementError) as exc:
                            raise QueryValidationError(
                                "a write in the atomic batch violated a database constraint or "
                                "value type; the whole batch was rolled back"
                            ) from exc
                        oks.append(
                            WriteBatchItemResult(
                                operation=stmt.op,
                                table=stmt.table,
                                affected_rows=(
                                    len(stmt.rows)
                                    if isinstance(stmt, (InsertStatement, UpsertStatement))
                                    else affected
                                ),
                                executed=True,
                            )
                        )
                    await session.commit()
        except Exception as exc:
            # All-or-nothing: nothing committed — report the same error for every
            # item so the caller sees the batch was rejected as a unit.
            message = public_error_message(exc)
            return [
                WriteBatchItemResult(operation=s.op, table=s.table, error=message)
                for s in statements
            ]
        for stmt, ok in zip(statements, oks):
            self._audit("", stmt, None, affected_rows=ok.affected_rows, rejected=False)
        return oks

    async def _execute_batch_item(
        self,
        statement: WriteStatement,
        approval_tokens: Optional[Dict[str, str]],
        approval_resolver: Optional["ApprovalResolver"],
    ) -> WriteBatchItemResult:
        token = approval_tokens.get(write_fingerprint(statement)) if approval_tokens else None
        try:
            return self._batch_ok(await self.execute(statement, approval_token=token))
        except ApprovalRequiredError as exc:
            # No pre-supplied token covered this write (or it was rejected). Give
            # an injected resolver one chance to obtain a token interactively,
            # then retry exactly once with it — mirrors
            # StructuredQueryService._execute_batch_item's identical shape.
            if approval_resolver is not None:
                resolved = await approval_resolver(statement, exc)
                if resolved is not None:
                    try:
                        return self._batch_ok(
                            await self.execute(statement, approval_token=resolved)
                        )
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
        )

    @staticmethod
    def _batch_error(statement: WriteStatement, exc: Exception) -> WriteBatchItemResult:
        return WriteBatchItemResult(
            operation=statement.op,
            table=statement.table,
            error=public_error_message(exc),
            approval_fingerprint=(
                exc.fingerprint if isinstance(exc, ApprovalRequiredError) else None
            ),
            approval_reasons=(exc.reasons if isinstance(exc, ApprovalRequiredError) else None),
        )

    async def _execute_in_transaction(
        self,
        statement: WriteStatement,
        table: sa.Table,
        dml,
        policy,
        approval_token: Optional[str],
    ) -> int:
        """One transaction: count -> cap -> approval -> mutate -> re-check cap ->
        commit. Any raise propagates to `session_scope`, which rolls back — so
        there is never a partial or over-cap committed write. Returns the affected
        row count."""
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

            try:
                result = await session.execute(dml)
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
            # Belt-and-suspenders: the statement's own rowcount must also be within
            # cap, so a race between the count and the mutation can't over-write.
            actual = (
                len(statement.rows)
                if isinstance(statement, (InsertStatement, UpsertStatement))
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
        return actual

    async def _count_affected(self, session, statement: WriteStatement, table: sa.Table) -> int:
        if isinstance(statement, (InsertStatement, UpsertStatement)):
            return len(statement.rows)
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(table)
            .where(
                _compile_where(
                    to_read_where(statement.where),
                    {statement.table: table},
                    {},
                    self._connection_dialect(),
                )
            )
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
        # TODO.md item 151: bound to this connection/principal, the write-side
        # sibling of StructuredQueryService._enforce_approval_gate's binding —
        # a token approved for this write on a different connection, or by/for
        # a different principal, is rejected even though the fingerprint matches.
        if approval_token and verify_approval_token(
            approval_token,
            fingerprint=fingerprint,
            key=app_config.approval_token_hmac_key,
            connection_id=self._connection_id,
            principal_subject=self._principal_subject,
            expected_kind=TOKEN_KIND_GRANT,
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
