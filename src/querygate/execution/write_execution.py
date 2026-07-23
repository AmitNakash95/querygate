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
from typing import List, Optional

import pydantic as pyd
import sqlalchemy as sa

from querygate.audit.events import AuditSurface
from querygate.audit.logger import audit_query
from querygate.compiler.sqlalchemy_compiler import _compile_where
from querygate.compiler.write_compiler import compile_write
from querygate.connections.engine import session_scope
from querygate.core.auth import Principal
from querygate.core.config import config as app_config
from querygate.core.exceptions import ApprovalRequiredError, QueryValidationError
from querygate.execution.approval import verify_approval_token, write_fingerprint
from querygate.execution.concurrency import concurrency_slot
from querygate.execution.write_preview import _reject_subquery_in_write_where
from querygate.policy.loader import get_policy
from querygate.policy.models import WritePolicy
from querygate.validation.write_policy_validation import validate_write_policy
from querygate.validation.write_schema_validation import validate_write_schema
from querygate.write_ast.models import InsertStatement, WriteStatement


class WriteResult(pyd.BaseModel):
    """Redaction-safe result of a committed governed write. No row/predicate
    values — the op, target table, and how many rows were affected."""

    operation: str
    table: str
    affected_rows: int
    executed: bool = True

    model_config = pyd.ConfigDict(extra="forbid")


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
        return WriteResult(operation=statement.op, table=table.name, affected_rows=affected)

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
        there is never a partial or over-cap committed write."""
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

            result = await session.execute(dml)
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
            return actual

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
