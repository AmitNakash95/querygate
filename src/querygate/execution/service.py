"""Validate, compile, and execute structured reads against one connection.

This is the single seam every REST route and MCP tool goes through — there
is no other path to a database from agent/client-facing code.
"""

from __future__ import annotations

import json
import time
from typing import Any, List, Optional, Tuple, Union

import pydantic as pyd
import sqlalchemy as sa

from querygate.audit.events import AuditSurface, normalize_query_shape
from querygate.audit.logger import audit_query
from querygate.catalog.loader import get_catalog_store
from querygate.catalog.models import (
    CatalogDraftObjectType,
    CatalogDraftTarget,
    CatalogUsageSignalKind,
    SensitivityClass,
    agent_visible,
    visible_relationships,
)
from querygate.catalog.retrieval import (
    CatalogCitation,
    CatalogSearchResponse,
    CompactCatalogCitation,
    policy_hidden_identifier_tokens,
    policy_safe_catalog_aliases,
    policy_safe_catalog_text,
    resolve_citation,
    search_catalog,
)
from querygate.catalog.usage import build_usage_signal, enqueue_usage_signal, should_emit_signal
from querygate.compiler.sqlalchemy_compiler import applied_column_masks, compile_structured_query
from querygate.connections.engine import get_engine, get_metadata, session_scope
from querygate.connections.models import DatabaseDialect
from querygate.connections.registry import get_registry
from querygate.connections.visibility import resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.config import config as app_config
from querygate.core.exceptions import (
    ApprovalRequiredError,
    CapacityTimeoutError,
    ConcurrencyLimitError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    QueueDepthExceededError,
    QueueFullError,
    QuotaExceededError,
    public_error_message,
)
from querygate.core.logging import get_logger, log_execution
from querygate.execution.admission import QueueMode, new_admission_id, resolve_wait_seconds
from querygate.execution.approval import (
    approval_required_reasons,
    query_fingerprint,
    sensitivity_approval_reasons,
    verify_approval_token,
)
from querygate.execution.concurrency import concurrency_slot
from querygate.execution.cost_estimation import (
    QueryCostEstimate,
    cost_estimate_violations,
    enforce_cost_estimate,
    estimate_postgres_query_cost,
)
from querygate.execution.quota import enforce_query_quota, record_query_quota_bytes
from querygate.metrics import (
    COST_ESTIMATION_WOULD_REJECT_TOTAL,
    QUERIES_REJECTED_TOTAL,
    QUERIES_TOTAL,
    QUERY_DURATION_SECONDS,
    QUERY_QUOTA_REJECTIONS_TOTAL,
    QUEUE_WAIT_SECONDS,
    classify_rejection,
)
from querygate.policy.models import CostEstimationMode, Policy
from querygate.query_ast.models import StructuredQuery
from querygate.schema.reflection import get_table_schema, list_live_tables, sanitize_table_name
from querygate.validation.policy_validation import validate_policy
from querygate.validation.schema_validation import validate_schema


class TableCatalogInfo(pyd.BaseModel):
    """Curated table-level metadata from an optional catalog overlay (see
    `querygate/catalog/`) — display-only, never used for policy enforcement.
    `relationships` is pre-filtered to targets the caller's resolved policy
    allows (see `catalog.models.visible_relationships`).
    """

    description: Optional[str] = None
    aliases: List[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    default_aggregation: Optional[str] = None
    allow_samples: bool = False
    relationships: List["RelationshipCatalogInfo"] = pyd.Field(default_factory=list)
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class RelationshipCatalogInfo(pyd.BaseModel):
    to_table: str
    column: str
    to_column: str
    description: Optional[str] = None
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class ColumnCatalogInfo(pyd.BaseModel):
    description: Optional[str] = None
    aliases: List[str] = pyd.Field(default_factory=list)
    sensitivity: SensitivityClass = SensitivityClass.NONE
    allow_samples: bool = False
    provenance: Union[CatalogCitation, CompactCatalogCitation]


class ColumnInfo(pyd.BaseModel):
    name: str
    type: str
    nullable: bool
    description: Optional[str] = None
    catalog: Optional[ColumnCatalogInfo] = None


class TableDescription(pyd.BaseModel):
    name: str
    columns: List[ColumnInfo]
    description: Optional[str] = None
    catalog: Optional[TableCatalogInfo] = None


class StructuredQueryResult(pyd.BaseModel):
    rows: List[dict]
    row_count: int
    truncated: bool
    limit: int
    offset: int
    # Agent-visible admission info (TODO.md item 35 phase 1). Optional so
    # existing direct-construction call sites (tests, `execute_many`'s error
    # path) don't need to supply them.
    admission_id: Optional[str] = None
    queue_wait_ms: Optional[int] = None


class ExplainResult(pyd.BaseModel):
    sql: str
    params: Optional[str] = None
    tables: List[str]
    limit: int


class BatchQueryItemResult(pyd.BaseModel):
    rows: Optional[List[dict]] = None
    row_count: Optional[int] = None
    truncated: Optional[bool] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    admission_id: Optional[str] = None
    admission_state: Optional[str] = None
    queue_wait_ms: Optional[int] = None
    error: Optional[str] = None


class BatchExplainItemResult(pyd.BaseModel):
    sql: Optional[str] = None
    params: Optional[str] = None
    tables: Optional[List[str]] = None
    limit: Optional[int] = None
    error: Optional[str] = None


def _clean_row_values(row: dict) -> dict:
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}


def _render_column_type(col_type: sa.types.TypeEngine) -> str:
    """`str(col.type)` renders SQLAlchemy's `NullType` as the string
    `"NULL"` — indistinguishable from an error, and misleading for an agent
    trying to understand a table's schema. MSSQL's `geography`/`geometry`
    (CLR user-defined types) reflect to `NullType` because the dialect has
    no native mapping for them, not because the column has no type — a
    real gap, verified against a live MSSQL server (TODO.md item 2).
    """
    if isinstance(col_type, sa.types.NullType):
        return "unsupported (driver could not determine this column's type — often a spatial/CLR type like geography or geometry)"
    return str(col_type)


def _cap_response_bytes(rows: List[dict], max_bytes: int) -> Tuple[List[dict], bool]:
    """Truncate `rows` before their serialized list exceeds `max_bytes`.

    Row *count* is capped by `Policy.max_limit`/`max_limit_aggregate`, but
    nothing else stops a wide TEXT/JSONB/BLOB column from making an
    otherwise-compliant query return a very large response body. Oversized
    rows are omitted—even when the first row alone exceeds the cap—so a
    caller cannot bypass the ceiling with one large value.
    """
    if max_bytes <= 0:
        return rows, False
    total = 2  # opening and closing brackets for the serialized list
    kept: List[dict] = []
    for row in rows:
        row_bytes = len(json.dumps(row, default=str).encode("utf-8"))
        separator_bytes = 2 if kept else 0  # json.dumps uses ", " between items
        if total + separator_bytes + row_bytes > max_bytes:
            return kept, True
        kept.append(row)
        total += separator_bytes + row_bytes
    return kept, False


def _compile_to_text(stmt: Any, *, include_literals: bool) -> Tuple[str, Optional[str]]:
    """Render a compiled statement to SQL text for audit logging / explain.

    Default posture (`include_literals=False`) is safe-by-default: SQL text
    always uses bind placeholders, and parameter values are redacted before
    being turned into the returned params string. Without this, WHERE-clause
    literals (an email, an SSN) end up verbatim in the audit log and in
    run_structured_queries(mode="explain")'s response, which contradicts the
    "audit records never contain row payloads" guarantee for anything
    expressible in a `where`. `include_literals=True` is an explicit
    per-policy opt-in (`Policy.log_query_literals`) for deployments that
    want full literal SQL for debugging.
    """
    compiled = stmt.compile()
    params = dict(compiled.params) if hasattr(compiled, "params") else {}

    if include_literals:
        try:
            literal_compiled = stmt.compile(compile_kwargs={"literal_binds": True})
            return str(literal_compiled), None
        except (
            Exception
        ):  # nosec B110 — best-effort literal rendering for audit only; some param types (e.g. arrays) can't render as literals, fall through to the parameterized form
            pass

    redacted = {k: "<redacted>" for k in params}
    return str(compiled), (str(redacted) if redacted else None)


class StructuredQueryService:
    """Validate, compile, and execute structured reads against `connection_id`."""

    def __init__(
        self,
        connection_id: str,
        principal: Optional[Principal] = None,
        surface: AuditSurface = "internal",
        template_id: Optional[str] = None,
        template_param_shape: Optional[list[str]] = None,
    ) -> None:
        self._connection_id = connection_id
        self._principal = principal
        self._surface = surface
        # Set only when this service was built to invoke a curated query
        # template (TODO.md item 48); threaded into the audit event so template
        # usage shows up distinctly from ad-hoc structured queries in the same
        # stream. `template_param_shape` carries parameter NAMES only.
        self._template_id = template_id
        self._template_param_shape = template_param_shape

    @property
    def _audit_operation(self) -> str:
        return "run_query_template" if self._template_id else "execute_structured_query"

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
        # The agent acting on behalf of `_principal_subject` (the human), for a
        # delegated (RFC 8693) request; None for a direct caller. See item 90.
        return self._principal.actor_subject if self._principal else None

    @property
    def _delegation_chain(self) -> Optional[List[str]]:
        return self._principal.delegation_chain if self._principal else None

    def _get_policy(self) -> Policy:
        # Resolves connection-level policy merged with any per-principal
        # override for this caller (see policy/loader.PolicyStore.get) — so
        # every call site sees a consistent view of "the policy that
        # applies to this principal on this connection", not just the
        # connection-wide default.
        _profile, policy = resolve_visible_connection(
            self._connection_id, principal=self._principal
        )
        return policy

    async def _validate_and_compile(
        self, query: StructuredQuery
    ) -> Tuple[sa.Select, int, dict, str]:
        policy = self._get_policy()
        validate_policy(query, policy, connection_id=self._connection_id)
        tables = await validate_schema(
            query, connection_id=self._connection_id, principal=self._principal
        )
        # Derived from the live engine, not ConnectionProfile.dialect — the
        # engine's own dialect is what actually executes the compiled SQL,
        # so this can't drift from reality (and lets tests swap in a SQLite
        # engine and get correct SQLite-flavored SQL, not Postgres/MSSQL SQL
        # that happens to fail against it).
        dialect = get_engine(self._connection_id).dialect.name
        stmt, limit = compile_structured_query(
            query, tables, policy, dialect=dialect, principal=self._principal
        )
        return stmt, limit, tables, dialect

    def _observe_cost_estimate(self, estimate: QueryCostEstimate, policy: Policy) -> None:
        """`CostEstimationMode.OBSERVE`: record what *would* have been
        rejected without blocking the query — lets an operator calibrate
        max_estimated_rows/max_estimated_cost against real traffic before
        switching a connection over to ENFORCE (see CostEstimationMode's
        docstring in policy/models.py).
        """
        violations = cost_estimate_violations(estimate, policy)
        if not violations:
            return
        COST_ESTIMATION_WOULD_REJECT_TOTAL.labels(connection=self._connection_id).inc()
        get_logger().bind(func="execute").warning(
            "cost_estimation.observed_would_reject",
            connection=self._connection_id,
            violations=violations,
            estimated_rows=estimate.estimated_rows,
            estimated_total_cost=estimate.estimated_total_cost,
        )

    def _enforce_approval_gate(
        self,
        estimate: Optional[QueryCostEstimate],
        policy: Policy,
        query: StructuredQuery,
        approval_token: Optional[str],
    ) -> None:
        """In-query human-in-the-loop gate (TODO.md item 92). If the query trips a
        policy approval trigger — a catalog sensitivity label (phase 2, dialect-
        agnostic) or the cost/row estimate (phase 1, Postgres; `estimate` may be
        None otherwise) — require a valid approval token bound to this exact
        query; otherwise raise `ApprovalRequiredError` so the caller can obtain
        one from a `query:approve` holder and re-submit. No-op when the gate is
        disabled or nothing triggers.
        """
        if not policy.approval_gate_enabled:
            return
        reasons = sensitivity_approval_reasons(query, policy, self._connection_id)
        if estimate is not None:
            reasons += approval_required_reasons(estimate, policy)
        if not reasons:
            return
        fingerprint = query_fingerprint(query)
        key = app_config.approval_token_hmac_key
        if verify_approval_token(approval_token or "", fingerprint=fingerprint, key=key):
            # Approved: the normal success audit records the execution; this
            # structured line ties the approval grant to the query in the log.
            get_logger().bind(func="execute").info(
                "approval.granted",
                connection=self._connection_id,
                fingerprint=fingerprint,
                principal=self._principal_subject,
                reasons=reasons,
            )
            return
        get_logger().bind(func="execute").warning(
            "approval.required",
            connection=self._connection_id,
            fingerprint=fingerprint,
            principal=self._principal_subject,
            reasons=reasons,
        )
        raise ApprovalRequiredError(
            "This query requires human approval before it can run: " + "; ".join(reasons),
            fingerprint=fingerprint,
            reasons=reasons,
        )

    def _usage_signal_targets(
        self, query: StructuredQuery
    ) -> List[Tuple[CatalogUsageSignalKind, CatalogDraftTarget]]:
        """Relationship targets use JoinSpec.on's own documented convention
        (``[LeftTable.Col, RightTable.Col]``) to decide which side is
        ``table``/``column`` vs. ``to_table``/``to_column`` — the same
        ordering the AST author already committed to, not a new inference.
        Cross-connection joins (``join.connection`` set) are skipped: their
        target table lives in a different connection's catalog.
        """

        targets: List[Tuple[CatalogUsageSignalKind, CatalogDraftTarget]] = [
            (
                CatalogUsageSignalKind.TABLE_USED,
                CatalogDraftTarget(
                    connection_id=self._connection_id,
                    object_type=CatalogDraftObjectType.TABLE,
                    table=query.from_table,
                ),
            )
        ]
        tables_seen = {query.from_table}
        for join in query.joins:
            if join.connection is not None:
                continue
            if join.table not in tables_seen:
                targets.append(
                    (
                        CatalogUsageSignalKind.TABLE_USED,
                        CatalogDraftTarget(
                            connection_id=self._connection_id,
                            object_type=CatalogDraftObjectType.TABLE,
                            table=join.table,
                        ),
                    )
                )
                tables_seen.add(join.table)
            left, right = join.on
            if "." not in left or "." not in right:
                continue
            left_table, left_column = left.split(".", 1)
            right_table, right_column = right.split(".", 1)
            targets.append(
                (
                    CatalogUsageSignalKind.RELATIONSHIP_USED,
                    CatalogDraftTarget(
                        connection_id=self._connection_id,
                        object_type=CatalogDraftObjectType.RELATIONSHIP,
                        table=left_table,
                        column=left_column,
                        to_table=right_table,
                        to_column=right_column,
                    ),
                )
            )
        return targets

    def _emit_usage_signals(self, query: StructuredQuery, *, admission_id: str) -> None:
        """Best-effort, non-blocking (TODO item 32C). Never raises into the
        response path: signal emission only touches an in-process buffer
        (``catalog.usage.enqueue_usage_signal``), never the catalog file
        lock — a background monitor does that batching separately.
        """

        if not app_config.semantic_memory_usage_signals_enabled or not app_config.catalog_file:
            return
        try:
            store = get_catalog_store()
            snapshot = store.get_schema_snapshot(self._connection_id)
            if snapshot is None:
                return
            for kind, target in self._usage_signal_targets(query):
                if not should_emit_signal(store, connection_id=self._connection_id, target=target):
                    continue
                signal = build_usage_signal(
                    connection_id=self._connection_id,
                    principal_subject=self._principal_subject,
                    target=target,
                    kind=kind,
                    schema_fingerprint=snapshot.fingerprint,
                    evidence_reference=f"admission:{admission_id}",
                )
                enqueue_usage_signal(signal)
        except Exception as exc:
            get_logger().bind(func="execute").warning(
                "semantic_memory.usage_signal_emit_failed", error_type=type(exc).__name__
            )

    @log_execution
    async def execute(
        self,
        query: StructuredQuery,
        *,
        queue_mode: Optional[QueueMode] = None,
        wait_timeout_seconds: Optional[float] = None,
        approval_token: Optional[str] = None,
    ) -> StructuredQueryResult:
        start = time.monotonic()
        admission_id = new_admission_id()
        query_shape = normalize_query_shape(query)
        sql = ""
        params: Optional[str] = None
        policy_validated = False
        queue_wait_ms: Optional[int] = None
        quota_reservation = None
        try:
            policy = self._get_policy()
            # Per-principal rate/byte quota (TODO.md item 50) — checked before
            # queuing or touching the database, so a rate-limited caller doesn't
            # even consume a concurrency slot. Raises QuotaExceededError (a
            # PolicyViolationError), handled by the outer `except` below.
            quota_reservation = enforce_query_quota(
                policy,
                connection_id=self._connection_id,
                principal_subject=self._principal_subject,
            )
            wait_seconds = resolve_wait_seconds(
                queue_mode=queue_mode,
                requested_wait_seconds=wait_timeout_seconds,
                policy_ceiling_seconds=policy.concurrency_wait_seconds,
            )
            queue_start = time.monotonic()
            try:
                async with concurrency_slot(
                    self._connection_id,
                    policy.max_concurrency,
                    wait_seconds,
                    principal_subject=self._principal_subject,
                    max_queue_depth=policy.max_queue_depth,
                    max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
                ):
                    queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
                    stmt, limit, _tables, dialect = await self._validate_and_compile(query)
                    policy_validated = True
                    sql, params = _compile_to_text(stmt, include_literals=policy.log_query_literals)

                    async with session_scope(self._connection_id, policy=policy) as session:
                        estimate: Optional[QueryCostEstimate] = None
                        if policy.estimate_needed and dialect == DatabaseDialect.POSTGRESQL:
                            estimate = await estimate_postgres_query_cost(
                                session, stmt, connection_id=self._connection_id
                            )
                            if estimate is not None and policy.cost_estimation_enabled:
                                if policy.cost_estimation_mode == CostEstimationMode.ENFORCE:
                                    enforce_cost_estimate(estimate, policy)
                                else:
                                    self._observe_cost_estimate(estimate, policy)
                        # Human-in-the-loop approval gate (item 92) runs after the
                        # hard cost gate (a query rejected by ENFORCE never reaches
                        # here). It combines the cost-estimate trigger (Postgres;
                        # `estimate` may be None otherwise) with the dialect-
                        # agnostic catalog sensitivity-label trigger, so a single
                        # approval token covers whatever tripped it.
                        if policy.approval_gate_enabled:
                            self._enforce_approval_gate(estimate, policy, query, approval_token)
                        result = await session.execute(stmt)
                        raw_rows = [dict(r) for r in result.mappings().all()]

                    rows = [_clean_row_values(r) for r in raw_rows]
                    row_limit_hit = len(rows) >= limit
                    rows, byte_cap_hit = _cap_response_bytes(rows, policy.max_response_bytes)
                    truncated = row_limit_hit or byte_cap_hit
                    response_bytes = len(json.dumps(rows, default=str).encode("utf-8"))
                    # Attribute this response's size to the quota window (item
                    # 50); a no-op when the quota is disabled for this policy.
                    record_query_quota_bytes(quota_reservation, response_bytes)
                    elapsed_seconds = time.monotonic() - start
                    QUEUE_WAIT_SECONDS.labels(
                        connection=self._connection_id, outcome="completed"
                    ).observe(queue_wait_ms / 1000)
                    audit_query(
                        connection_id=self._connection_id,
                        sql=sql,
                        params=params,
                        intent=query.intent,
                        row_count=len(rows),
                        duration_ms=int(elapsed_seconds * 1000),
                        principal=self._principal_subject,
                        principal_scopes=self._principal_scopes,
                        actor=self._principal_actor,
                        delegation_chain=self._delegation_chain,
                        auth_method=self._auth_method,
                        surface=self._surface,
                        query_shape=query_shape,
                        response_bytes=response_bytes,
                        truncated=truncated,
                        policy_decision="allowed",
                        admission_id=admission_id,
                        queue_wait_ms=queue_wait_ms,
                        admission_state="completed",
                        operation=self._audit_operation,
                        template_id=self._template_id,
                        template_param_shape=self._template_param_shape,
                        masked_columns=applied_column_masks(query, policy),
                    )
                    self._emit_usage_signals(query, admission_id=admission_id)
                    QUERIES_TOTAL.labels(connection=self._connection_id, status="success").inc()
                    QUERY_DURATION_SECONDS.labels(connection=self._connection_id).observe(
                        elapsed_seconds
                    )
                    return StructuredQueryResult(
                        rows=rows,
                        row_count=len(rows),
                        truncated=truncated,
                        limit=limit,
                        offset=query.offset,
                        admission_id=admission_id,
                        queue_wait_ms=queue_wait_ms,
                    )
            except ConcurrencyLimitError as exc:
                queue_wait_ms = int((time.monotonic() - queue_start) * 1000)
                if isinstance(exc, QueueDepthExceededError):
                    QUEUE_WAIT_SECONDS.labels(
                        connection=self._connection_id, outcome="queue_full"
                    ).observe(queue_wait_ms / 1000)
                    raise QueueFullError(str(exc), admission_id=admission_id) from exc
                QUEUE_WAIT_SECONDS.labels(
                    connection=self._connection_id, outcome="capacity_timeout"
                ).observe(queue_wait_ms / 1000)
                raise CapacityTimeoutError(
                    str(exc), admission_id=admission_id, queue_wait_ms=queue_wait_ms
                ) from exc
        except Exception as exc:
            error_category = (
                "not_found" if isinstance(exc, NotFoundError) else classify_rejection(exc)
            )
            audit_query(
                connection_id=self._connection_id,
                sql=sql,
                params=params,
                intent=query.intent,
                duration_ms=int((time.monotonic() - start) * 1000),
                principal=self._principal_subject,
                principal_scopes=self._principal_scopes,
                actor=self._principal_actor,
                delegation_chain=self._delegation_chain,
                auth_method=self._auth_method,
                surface=self._surface,
                query_shape=query_shape,
                error_category=error_category,
                policy_decision=(
                    "allowed"
                    if policy_validated
                    else (
                        "denied"
                        if error_category in ("policy", "schema", "not_found", "quota")
                        else "unknown"
                    )
                ),
                rejected=True,
                rejection_reason=str(exc),
                admission_id=admission_id,
                queue_wait_ms=queue_wait_ms,
                admission_state=getattr(exc, "admission_state", None),
                operation=self._audit_operation,
                template_id=self._template_id,
                template_param_shape=self._template_param_shape,
            )
            QUERIES_TOTAL.labels(connection=self._connection_id, status="rejected").inc()
            QUERIES_REJECTED_TOTAL.labels(
                connection=self._connection_id, reason=classify_rejection(exc)
            ).inc()
            if isinstance(exc, QuotaExceededError):
                QUERY_QUOTA_REJECTIONS_TOTAL.labels(
                    connection=self._connection_id, quota_kind=exc.quota_kind
                ).inc()
            raise

    async def execute_many(
        self,
        queries: List[StructuredQuery],
        *,
        queue_mode: Optional[QueueMode] = None,
        wait_timeout_seconds: Optional[float] = None,
    ) -> List[BatchQueryItemResult]:
        """Run each query independently; one failure doesn't drop the rest of the batch."""
        results: List[BatchQueryItemResult] = []
        for query in queries:
            try:
                result = await self.execute(
                    query, queue_mode=queue_mode, wait_timeout_seconds=wait_timeout_seconds
                )
                results.append(BatchQueryItemResult(**result.model_dump()))
            except Exception as exc:
                results.append(
                    BatchQueryItemResult(
                        error=public_error_message(exc),
                        admission_id=getattr(exc, "admission_id", None),
                        admission_state=getattr(exc, "admission_state", None),
                        queue_wait_ms=getattr(exc, "queue_wait_ms", None),
                    )
                )
        return results

    @log_execution
    async def explain(self, query: StructuredQuery) -> ExplainResult:
        """Validate + compile without executing; returns the SQL that would run.

        Deliberately never opens a DB session (see
        test_explain_does_not_open_a_db_session), so — unlike `execute()` —
        this does not run the `max_estimated_rows`/`max_estimated_cost`
        pre-execution cost check either: that check needs a live EXPLAIN
        round-trip against Postgres, which would make "explain" a
        DB-touching operation with its own concurrency/availability
        footprint instead of a pure, always-cheap compile preview.
        """
        policy = self._get_policy()
        async with concurrency_slot(
            self._connection_id, policy.max_concurrency, policy.concurrency_wait_seconds
        ):
            stmt, limit, tables, _dialect = await self._validate_and_compile(query)
            sql, params = _compile_to_text(stmt, include_literals=policy.log_query_literals)
            return ExplainResult(sql=sql, params=params, tables=sorted(tables), limit=limit)

    async def explain_many(self, queries: List[StructuredQuery]) -> List[BatchExplainItemResult]:
        """Explain each query independently; one failure doesn't drop the rest."""
        results: List[BatchExplainItemResult] = []
        for query in queries:
            try:
                result = await self.explain(query)
                results.append(BatchExplainItemResult(**result.model_dump()))
            except Exception as exc:
                results.append(BatchExplainItemResult(error=public_error_message(exc)))
        return results

    @log_execution
    async def list_tables(self) -> List[str]:
        """Known/reflected table names for this connection, filtered by policy.

        Prefers already-reflected metadata + any `known_tables` seed list on
        the connection profile; falls back to a live INFORMATION_SCHEMA query
        when neither is available.
        """
        policy = self._get_policy()
        profile = get_registry().get(self._connection_id)
        metadata = get_metadata(self._connection_id)
        names = {t.split(".")[-1] for t in metadata.tables.keys()}
        if profile.known_tables:
            names.update(profile.known_tables)
        else:
            names.update(await list_live_tables(self._connection_id))
        return sorted(name for name in names if policy.table_allowed(name))

    @log_execution
    async def search_catalog(
        self, query: str, *, max_results: int = 5, verbose_provenance: bool = False
    ) -> CatalogSearchResponse:
        """Retrieve compact semantic context without touching the database.

        The catalog retrieval layer applies this principal's resolved policy
        before it tokenizes or ranks candidates. It is deliberately separate
        from query validation/execution: a missing or empty catalog returns no
        results and can never weaken or block ordinary structured queries.

        Each hit's citation is compact (status + precedence) unless
        `verbose_provenance` is set — see `catalog.retrieval.search_catalog`.
        """

        policy = self._get_policy()
        try:
            return search_catalog(
                get_catalog_store(),
                connection_id=self._connection_id,
                policy=policy,
                query=query,
                max_results=max_results,
                verbose_provenance=verbose_provenance,
            )
        except ValueError as exc:
            raise QueryValidationError(str(exc)) from exc

    @log_execution
    async def describe_table(
        self, table_name: str, *, verbose_provenance: bool = False
    ) -> TableDescription:
        policy = self._get_policy()
        try:
            sanitize_table_name(table_name)
        except ValueError as exc:
            raise QueryValidationError(str(exc)) from exc
        if not policy.table_allowed(table_name):
            raise PolicyViolationError(
                f"Table {table_name!r} is not accessible under the active policy"
            )
        engine = get_engine(self._connection_id)
        table = await get_table_schema(table_name, self._connection_id, engine)
        # Denied columns are already excluded from `columns` below, so their
        # catalog entries never get looked up in the first place — no
        # separate column-level filtering is needed here.
        catalog_store = get_catalog_store()
        catalog_entry = catalog_store.get_table(self._connection_id, table_name)
        schema_snapshot = catalog_store.get_schema_snapshot(self._connection_id)
        current_schema_fingerprint = (
            schema_snapshot.fingerprint if schema_snapshot is not None else None
        )
        hidden_identifier_tokens = policy_hidden_identifier_tokens(
            catalog_store, connection_id=self._connection_id, policy=policy
        )

        def column_catalog(column_name: str) -> Optional[ColumnCatalogInfo]:
            if catalog_entry is None:
                return None
            entry = catalog_entry.column(column_name)
            if entry is None or not agent_visible(entry.provenance):
                return None
            return ColumnCatalogInfo(
                description=policy_safe_catalog_text(entry.description, hidden_identifier_tokens),
                aliases=policy_safe_catalog_aliases(entry.aliases, hidden_identifier_tokens),
                sensitivity=entry.sensitivity,
                allow_samples=entry.allow_samples,
                provenance=resolve_citation(
                    entry.provenance, current_schema_fingerprint, verbose=verbose_provenance
                ),
            )

        columns = [
            ColumnInfo(
                name=col.name,
                type=_render_column_type(col.type),
                nullable=bool(col.nullable),
                description=col.comment,
                catalog=column_catalog(col.name),
            )
            for col in table.columns
            if policy.column_allowed(table_name, col.name)
        ]
        visible_relationship_entries = (
            visible_relationships(catalog_entry, policy, from_table=table_name)
            if catalog_entry is not None
            else []
        )
        table_catalog = (
            TableCatalogInfo(
                description=policy_safe_catalog_text(
                    catalog_entry.description, hidden_identifier_tokens
                ),
                aliases=policy_safe_catalog_aliases(
                    catalog_entry.aliases, hidden_identifier_tokens
                ),
                sensitivity=catalog_entry.sensitivity,
                default_aggregation=policy_safe_catalog_text(
                    catalog_entry.default_aggregation, hidden_identifier_tokens
                ),
                allow_samples=catalog_entry.allow_samples,
                relationships=[
                    RelationshipCatalogInfo(
                        to_table=relationship.to_table,
                        column=relationship.column,
                        to_column=relationship.to_column,
                        description=policy_safe_catalog_text(
                            relationship.description, hidden_identifier_tokens
                        ),
                        provenance=resolve_citation(
                            relationship.provenance,
                            current_schema_fingerprint,
                            verbose=verbose_provenance,
                        ),
                    )
                    for relationship in visible_relationship_entries
                ],
                provenance=resolve_citation(
                    catalog_entry.provenance,
                    current_schema_fingerprint,
                    verbose=verbose_provenance,
                ),
            )
            if catalog_entry and agent_visible(catalog_entry.provenance)
            else None
        )
        return TableDescription(
            name=table.name, columns=columns, description=table.comment, catalog=table_catalog
        )
