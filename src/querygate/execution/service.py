"""Validate, compile, and execute structured reads against one connection.

This is the single seam every REST route and MCP tool goes through — there
is no other path to a database from agent/client-facing code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple, Union

import pydantic as pyd
import sqlalchemy as sa
from sqlalchemy.exc import DataError, ProgrammingError

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
    TOKEN_KIND_GRANT,
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
    estimate_mssql_query_cost,
    estimate_postgres_query_cost,
)
from querygate.execution.quota import (
    QuotaReservation,
    enforce_query_quota,
    record_query_quota_bytes,
)
from querygate.metrics import (
    COST_ESTIMATION_WOULD_REJECT_TOTAL,
    QUERIES_REJECTED_TOTAL,
    QUERIES_TOTAL,
    QUERY_DURATION_SECONDS,
    QUERY_QUOTA_REJECTIONS_TOTAL,
    QUEUE_WAIT_SECONDS,
    VERDICT_DURATION_SECONDS,
    VERDICTS_TOTAL,
    classify_rejection,
)
from querygate.policy.models import CostEstimationMode, Policy
from querygate.query_ast.models import JoinSpec, Predicate, StructuredQuery
from querygate.schema.reflection import get_table_schema, list_live_tables, sanitize_table_name
from querygate.validation.policy_validation import validate_policy
from querygate.validation.schema_validation import (
    declared_cte_names,
    iter_query_scopes,
    resolve_scope_connections,
    validate_schema,
)


def _join_relationship_pair(join: JoinSpec) -> Optional[Tuple[str, str]]:
    """The `[LeftTable.Col, RightTable.Col]` pair a join asserts, or None.

    Item 32C's `RELATIONSHIP_USED` signal records one column-to-column
    relationship, so this answers "does this join assert exactly one, and which?"

    Both spellings of an equality join return the same pair, deliberately. Item
    103 made `condition` a second way to write `ON a.x = b.y`, and reading only
    `on` would have meant the newer spelling silently taught the catalog nothing
    — the same spelling asymmetry `audit/events.py`'s `value_column` exists to
    prevent, one layer over. A range/temporal join, a multi-predicate condition
    and a cross join genuinely assert no single pair, and return None.
    """
    if join.on is not None:
        return join.on[0], join.on[1]
    condition = join.condition
    if (
        isinstance(condition, Predicate)
        and condition.op == "eq"
        and condition.col is not None
        and condition.value_col is not None
    ):
        return condition.col, condition.value_col
    return None


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


class VerdictPlan(pyd.BaseModel):
    """Only present when `Policy.verdict_include_plan` opts in; omitted by
    default since the compiled SQL and touched-table list are themselves a
    discovery channel for a caller who couldn't otherwise see this schema."""

    sql: str
    tables: List[str]


class VerdictResult(pyd.BaseModel):
    """TODO.md item 133: would `query` be allowed for this principal, without
    executing it. `reason`/`message` are deliberately coarse — see
    `StructuredQueryService.verdict`'s docstring for why a denial never
    distinguishes policy from schema."""

    allowed: bool
    reason: Optional[Literal["not-available-to-you"]] = None
    message: Optional[str] = None
    plan: Optional[VerdictPlan] = None


class BatchVerdictItemResult(pyd.BaseModel):
    allowed: Optional[bool] = None
    reason: Optional[Literal["not-available-to-you"]] = None
    message: Optional[str] = None
    plan: Optional[VerdictPlan] = None
    # A system-level failure (quota exhausted, connection at capacity) rather
    # than a verdict about the query's shape — mirrors BatchExplainItemResult's
    # per-item error tolerance.
    error: Optional[str] = None


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
    # Populated only when `error` is specifically an ApprovalRequiredError
    # (TODO.md item 92/128) — lets a caller distinguish "this item needs
    # human approval" from any other rejection without parsing `error`'s
    # free-text message, and carries what a caller needs to request it (the
    # MCP MRTR port builds one InputRequiredResult input_request per item
    # that sets these; REST's single-query path already surfaces the same
    # two fields via ApprovalRequiredError's 428 response).
    approval_fingerprint: Optional[str] = None
    approval_reasons: Optional[List[str]] = None


# An injected, transport-specific way to obtain an in-query approval token when
# a batch query trips the approval gate (item 92). Given the offending query and
# its `ApprovalRequiredError`, return a valid token to retry with, or `None` to
# leave the query rejected. The MCP transport supplies one backed by
# `Context.elicit` (interactive human approval); the service itself stays
# transport-agnostic and never imports MCP.
ApprovalResolver = Callable[["StructuredQuery", ApprovalRequiredError], Awaitable[Optional[str]]]


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
    ) -> Tuple[sa.Select, int, dict, str, Policy, Dict[int, Dict[str, str]]]:
        policy = self._get_policy()
        # TODO.md item 156: the same per-scope table-to-connection map item 155
        # threads through the approval gate, computed HERE — before policy
        # validation, and therefore before schema validation ever reflects
        # anything — because `resolve_query_table_connections` (which this
        # calls, once per scope) touches only the in-memory connection
        # registry/policy store, never a database, so computing it this early
        # doesn't weaken validation/policy_validation.py's documented "runs
        # BEFORE schema_validation.py reflects anything" ordering. Lets
        # `validate_policy` consult a cross-connection join's table's own
        # connection's Policy (column masks, mandatory row filters, table/
        # column deny-lists) alongside the primary connection's, instead of
        # only ever seeing `policy` — closing the gap item 155's own follow-up
        # recorded as item 156.
        early_scope_connections = resolve_scope_connections(
            query, self._connection_id, principal=self._principal
        )
        # TODO.md item 145: `validate_policy` returns the purpose-narrowed
        # effective Policy (unchanged if the query declares no purpose, or if
        # this connection hasn't configured any). Rebinding `policy` here is
        # what makes that narrowing actually reach the compiler below — its
        # `mandatory_row_filters`/`column_masks` are read from this same
        # local, not re-resolved from `self._get_policy()`.
        policy = validate_policy(
            query,
            policy,
            connection_id=self._connection_id,
            principal=self._principal,
            scope_connections=early_scope_connections,
        )
        # scope_tables collects each nested value_subquery's reflected tables
        # (item 97), keyed by node id, so the compiler can render IN (subquery).
        # Empty for a non-nested query.
        scope_tables: dict = {}
        # scope_connections (item 155) collects each scope's own table-to-
        # connection map — the same map schema validation used to pick which
        # connection a cross-connection join's table reflects against — so the
        # approval gate's catalog sensitivity-label trigger can look a joined
        # table up in the connection it actually resolved to, not just this
        # query's top-level `self._connection_id`. Recomputed here (rather than
        # reusing `early_scope_connections` above) because `validate_schema` is
        # the authority that pairs this map with the reflected `sa.Table`
        # objects it also produces; the two calls are deterministic pure
        # resolutions over the same AST and registry state, so they always
        # agree — `test_schema_validation.py`/`test_policy_validation.py`
        # exercise each independently.
        scope_connections: Dict[int, Dict[str, str]] = {}
        tables = await validate_schema(
            query,
            connection_id=self._connection_id,
            principal=self._principal,
            scope_tables=scope_tables,
            scope_connections=scope_connections,
        )
        # Derived from the live engine, not ConnectionProfile.dialect — the
        # engine's own dialect is what actually executes the compiled SQL,
        # so this can't drift from reality (and lets tests swap in a SQLite
        # engine and get correct SQLite-flavored SQL, not Postgres/MSSQL SQL
        # that happens to fail against it).
        dialect = get_engine(self._connection_id).dialect.name
        stmt, limit = compile_structured_query(
            query,
            tables,
            policy,
            dialect=dialect,
            principal=self._principal,
            subquery_tables=scope_tables,
            connection_id=self._connection_id,
            scope_connections=scope_connections,
        )
        # Every scope's effective table names, not just the outer scope's — the
        # explain response reports what the statement will READ, and since item 104
        # a set operation's other arms (and, since item 97, a nested subquery) are
        # named in the returned SQL but were missing from `tables`, so two fields of
        # one response contradicted each other (TODO.md item 121).
        touched = {name for scoped in scope_tables.values() for name in scoped}
        # The purpose-narrowed `policy` (item 145) is returned too — not just
        # used locally — so a caller auditing this query (e.g. `execute()`'s
        # `applied_column_masks(query, policy)`) reports a purpose-added mask,
        # not the un-narrowed base policy's view (found by
        # `security-invariant-reviewer`, 2026-08-05: the mask was correctly
        # APPLIED to the compiled SQL either way, but the audit event
        # understated which columns were actually masked).
        return stmt, limit, touched or set(tables), dialect, policy, scope_connections

    async def _estimate_cost(
        self, dialect: DatabaseDialect, session, stmt: sa.Select
    ) -> Optional[QueryCostEstimate]:
        """Pre-execution cost estimate for the compiled query, per dialect:
        Postgres plans it inline in the open session (`EXPLAIN`); MSSQL needs a
        dedicated SHOWPLAN_XML connection. Both fail open (return None). Any other
        dialect has no estimator yet — the query proceeds under the reactive
        guardrails."""
        if dialect == DatabaseDialect.POSTGRESQL:
            return await estimate_postgres_query_cost(
                session, stmt, connection_id=self._connection_id
            )
        if dialect == DatabaseDialect.MSSQL:
            return await estimate_mssql_query_cost(
                get_engine(self._connection_id), stmt, connection_id=self._connection_id
            )
        return None

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
        scope_connections: Optional[Dict[int, Dict[str, str]]] = None,
    ) -> None:
        """In-query human-in-the-loop gate (TODO.md item 92). If the query trips a
        policy approval trigger — a catalog sensitivity label (phase 2, dialect-
        agnostic) or the cost/row estimate (phase 1, Postgres; `estimate` may be
        None otherwise) — require a valid approval token bound to this exact
        query; otherwise raise `ApprovalRequiredError` so the caller can obtain
        one from a `query:approve` holder and re-submit. No-op when the gate is
        disabled or nothing triggers.

        The token is verified against this connection/principal (TODO.md item
        151), not just the fingerprint: `self._connection_id` is set once, in
        `__init__`, from this service's own top-level connection — `JoinSpec.
        connection` (a cross-connection join's own, separate field) is read
        only locally inside `validation/schema_validation.py`'s resolution and
        never assigned to `self._connection_id`, so a token minted for the
        byte-identical AST on a different connection is rejected, and a token
        bound to a different principal at issue time is rejected too.

        `scope_connections` (TODO.md item 155) is `_validate_and_compile`'s
        per-scope table-to-connection map, passed straight through to
        `sensitivity_approval_reasons` so a cross-connection join's table is
        looked up in the catalog of the connection it actually resolved to,
        not always `self._connection_id`. `None` (the default, for any future
        caller that hasn't run schema validation first) falls back to the
        pre-155 behavior of resolving every table against `self._connection_id`.
        """
        if not policy.approval_gate_enabled:
            return
        reasons = sensitivity_approval_reasons(
            query, policy, self._connection_id, scope_connections
        )
        if estimate is not None:
            reasons += approval_required_reasons(estimate, policy)
        if not reasons:
            return
        fingerprint = query_fingerprint(query)
        key = app_config.approval_token_hmac_key
        if verify_approval_token(
            approval_token or "",
            fingerprint=fingerprint,
            key=key,
            connection_id=self._connection_id,
            principal_subject=self._principal_subject,
            expected_kind=TOKEN_KIND_GRANT,
        ):
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
        """Relationship targets use the join's own documented convention
        (``[LeftTable.Col, RightTable.Col]``) to decide which side is
        ``table``/``column`` vs. ``to_table``/``to_column`` — the same
        ordering the AST author already committed to, not a new inference.
        Cross-connection joins (``join.connection`` set) are skipped: their
        target table lives in a different connection's catalog.

        Walks every scope (``iter_query_scopes``), so a set-operation arm
        (item 104) or a nested ``IN (subquery)`` (item 97) teaches the catalog the
        tables and relationships it actually used. Reading only the outer scope
        made the second arm of a union invisible to 32C — a fidelity gap, not a
        safety one, but the same "a consumer assumed one scope" class the audit
        shape and the approval gate had.
        """

        targets: List[Tuple[CatalogUsageSignalKind, CatalogDraftTarget]] = []
        tables_seen: set = set()

        def _add_table(name: str) -> None:
            if name in tables_seen:
                return
            tables_seen.add(name)
            targets.append(
                (
                    CatalogUsageSignalKind.TABLE_USED,
                    CatalogDraftTarget(
                        connection_id=self._connection_id,
                        object_type=CatalogDraftObjectType.TABLE,
                        table=name,
                    ),
                )
            )

        # A cte name (item 105) is a stage this statement computes, not an object in
        # the database — teaching 32C that a table by that name exists would put a
        # phantom into the catalog that no refresh could ever reconcile. The real
        # tables are still learned: each block's body is its own scope in this walk.
        cte_names = declared_cte_names(query)

        for _depth, scope in iter_query_scopes(query):
            if scope.from_table.casefold() not in cte_names:
                _add_table(scope.from_table)
            for join in scope.joins:
                if join.connection is not None or join.table.casefold() in cte_names:
                    continue
                _add_table(join.table)
                pair = _join_relationship_pair(join)
                if pair is None:
                    # A cross join, or a condition that expresses something other
                    # than one column-equals-column relationship (a range/temporal
                    # join, or a multi-predicate tree) — there is no single
                    # [Left.Col, Right.Col] pair for a RELATIONSHIP_USED signal to
                    # carry. Skip it; the TABLE_USED signals above still stand.
                    # `continue`, never an unpack: this list is built eagerly, so
                    # raising here would discard the whole batch, including the
                    # table signals already collected.
                    continue
                left, right = pair
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
        _reserved_quota: Optional[QuotaReservation] = None,
        _admission_id: Optional[str] = None,
        on_wait_start: Optional[Callable[[float], Awaitable[None]]] = None,
        on_admitted: Optional[Callable[[int], Awaitable[None]]] = None,
        on_session_identifier: Optional[Callable[[str], None]] = None,
    ) -> StructuredQueryResult:
        """`_admission_id`/`on_wait_start`/`on_admitted`/`on_session_identifier`
        are used only by the async execution lifecycle (TODO.md item 35 phase
        3, `execution/async_execution.py`) and MCP progress notifications
        (`mcp/tools/query.py`): `_admission_id` lets the async lifecycle
        generate and return the id in its `202` response BEFORE this method
        (running in a background task) has even started, rather than
        discovering the id only after the fact; `on_wait_start`/`on_admitted`
        observe "about to wait up to N seconds for a concurrency slot" and
        "the slot was acquired after N ms, execution is now running" — the
        two-point signal MCP's `Context.report_progress` uses (item 35 phase
        3 deliberately doesn't slice the wait into periodic ticks: that would
        mean retrying `concurrency_slot`'s acquire in shorter increments,
        risking a regression in the "a caller cannot extend its wait past the
        operator's ceiling" guarantee for a DX-only feature); `on_session_identifier`
        is "here is the dialect-captured session identifier a later cancel
        call can target". The ordinary synchronous callers (REST/MCP's
        `mode='explain'` path, or a caller passing none of these) see
        unchanged behavior.
        """
        start = time.monotonic()
        admission_id = _admission_id if _admission_id is not None else new_admission_id()
        query_shape = normalize_query_shape(query)
        sql = ""
        params: Optional[str] = None
        policy_validated = False
        queue_wait_ms: Optional[int] = None
        quota_reservation = None
        # None until `_get_policy()` succeeds below — the outer `except` (item
        # 145) must not assume `policy` is bound, since a failure resolving the
        # policy itself (e.g. an unknown connection) is exactly one of the
        # exceptions that except clause catches.
        policy: Optional[Policy] = None
        try:
            policy = self._get_policy()
            # Per-principal rate/byte quota (TODO.md item 50) — checked before
            # queuing or touching the database, so a rate-limited caller doesn't
            # even consume a concurrency slot. Raises QuotaExceededError (a
            # PolicyViolationError), handled by the outer `except` below.
            #
            # `_reserved_quota` is set only on an in-session approval retry
            # (item 107): the first attempt already reserved a unit before it
            # paused for approval, so the retry reuses that reservation instead of
            # reserving (and counting) a second unit for one logical query.
            if _reserved_quota is not None:
                quota_reservation = _reserved_quota
            else:
                quota_reservation = await enforce_query_quota(
                    policy,
                    connection_id=self._connection_id,
                    principal_subject=self._principal_subject,
                )
            wait_seconds = resolve_wait_seconds(
                queue_mode=queue_mode,
                requested_wait_seconds=wait_timeout_seconds,
                policy_ceiling_seconds=policy.concurrency_wait_seconds,
            )
            if on_wait_start is not None:
                await on_wait_start(wait_seconds)
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
                    if on_admitted is not None:
                        await on_admitted(queue_wait_ms)
                    # TODO.md item 145: rebind the outer `policy` to the
                    # purpose-narrowed effective policy too, so everything
                    # downstream in this method (session guardrails, and
                    # `applied_column_masks` at the audit call below) sees the
                    # same narrowing the compiler already used — not just this
                    # method's own now-stale un-narrowed local.
                    stmt, limit, _tables, dialect, policy, scope_connections = (
                        await self._validate_and_compile(query)
                    )
                    policy_validated = True
                    sql, params = _compile_to_text(stmt, include_literals=policy.log_query_literals)

                    async with session_scope(
                        self._connection_id,
                        policy=policy,
                        session_identifier_sink=on_session_identifier,
                    ) as session:
                        estimate: Optional[QueryCostEstimate] = None
                        if policy.estimate_needed:
                            estimate = await self._estimate_cost(dialect, session, stmt)
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
                            self._enforce_approval_gate(
                                estimate, policy, query, approval_token, scope_connections
                            )
                        try:
                            result = await session.execute(stmt)
                        except (DataError, ProgrammingError) as exc:
                            # The database refused the statement itself. Since
                            # every identifier was already reflected and policy-
                            # checked, what reaches here is a value/type/operator
                            # problem the caller expressed — e.g. multiplying a
                            # text column (item 100 made arithmetic reachable, so
                            # this became an ordinary caller mistake rather than
                            # an exotic one). That is a 4xx, not a server fault,
                            # and it is mapped exactly like the write path maps
                            # constraint violations (`write_execution.py`). The
                            # message names the CLASS of problem and never echoes
                            # the driver text, which can carry table/column names
                            # and the failing values — so neither the response nor
                            # the audit's rejection_reason leaks schema or data.
                            raise QueryValidationError(
                                "the database could not execute this query as expressed — an "
                                "operator, function, or value type is not valid for the "
                                "referenced columns (for example arithmetic on a text column). "
                                "The underlying database error is not returned."
                            ) from exc
                        raw_rows = [dict(r) for r in result.mappings().all()]

                    rows = [_clean_row_values(r) for r in raw_rows]
                    row_limit_hit = len(rows) >= limit
                    rows, byte_cap_hit = _cap_response_bytes(rows, policy.max_response_bytes)
                    truncated = row_limit_hit or byte_cap_hit
                    response_bytes = len(json.dumps(rows, default=str).encode("utf-8"))
                    # Attribute this response's size to the quota window (item
                    # 50); a no-op when the quota is disabled for this policy.
                    await record_query_quota_bytes(quota_reservation, response_bytes)
                    elapsed_seconds = time.monotonic() - start
                    QUEUE_WAIT_SECONDS.labels(
                        connection=self._connection_id, outcome="completed"
                    ).observe(queue_wait_ms / 1000)
                    audit_query(
                        connection_id=self._connection_id,
                        sql=sql,
                        params=params,
                        intent=query.intent,
                        purpose=(
                            query.purpose
                            if policy is not None and policy.allowed_purposes
                            else None
                        ),
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
                        masked_columns=applied_column_masks(
                            query,
                            policy,
                            connection_id=self._connection_id,
                            scope_connections=scope_connections,
                            principal=self._principal,
                        ),
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
                # A conservative Retry-After hint (REST 429, item 35 phase 3):
                # the connection's own configured wait ceiling, not a made-up
                # constant — the operator's own guidance for how long a slot
                # might take to free up. max(1, ...) so a ceiling under 1s
                # (or 0, if an operator ever configures that) never yields a
                # 0-second Retry-After, which some HTTP clients treat as
                # "retry immediately" rather than "retry very soon".
                retry_after_seconds = max(1, int(policy.concurrency_wait_seconds))
                if isinstance(exc, QueueDepthExceededError):
                    QUEUE_WAIT_SECONDS.labels(
                        connection=self._connection_id, outcome="queue_full"
                    ).observe(queue_wait_ms / 1000)
                    raise QueueFullError(
                        str(exc), admission_id=admission_id, retry_after_seconds=retry_after_seconds
                    ) from exc
                QUEUE_WAIT_SECONDS.labels(
                    connection=self._connection_id, outcome="capacity_timeout"
                ).observe(queue_wait_ms / 1000)
                raise CapacityTimeoutError(
                    str(exc),
                    admission_id=admission_id,
                    queue_wait_ms=queue_wait_ms,
                    retry_after_seconds=retry_after_seconds,
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
                purpose=(query.purpose if policy is not None and policy.allowed_purposes else None),
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
            if isinstance(exc, ApprovalRequiredError):
                # Hand the already-spent quota reservation to an in-session retry
                # so it doesn't reserve a second unit (item 107). The estimate/
                # sensitivity trigger re-evaluates on the retry regardless.
                exc.quota_reservation = quota_reservation
            raise

    async def execute_many(
        self,
        queries: List[StructuredQuery],
        *,
        queue_mode: Optional[QueueMode] = None,
        wait_timeout_seconds: Optional[float] = None,
        approval_tokens: Optional[Dict[str, str]] = None,
        approval_resolver: Optional[ApprovalResolver] = None,
        on_wait_start: Optional[Callable[[float], Awaitable[None]]] = None,
        on_admitted: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> List[BatchQueryItemResult]:
        """Run each query independently; one failure doesn't drop the rest of the batch.

        `approval_tokens` maps a query fingerprint to its in-query approval token
        (item 92), so a batch can carry the per-query grants an approval-gated
        query needs — the REST token-flow analogue for `execute()`'s
        `approval_token`. A query with no matching token stays fail-closed: it
        surfaces its own `ApprovalRequiredError` as that item's `error` without
        affecting the rest of the batch. Each token is still verified against
        that exact query's fingerprint inside `execute()`, so a token can't be
        replayed onto a different query in the same batch.

        `approval_resolver` is an optional last-resort way to obtain a token
        interactively when a query trips the gate and no pre-supplied token
        covers it — the MCP transport passes one backed by `Context.elicit`. It
        keeps all batch/error shaping here (one source of truth) while the
        transport-specific approval interaction is injected, not imported.
        """
        results: List[BatchQueryItemResult] = []
        for query in queries:
            results.append(
                await self._execute_batch_item(
                    query,
                    queue_mode=queue_mode,
                    wait_timeout_seconds=wait_timeout_seconds,
                    approval_tokens=approval_tokens,
                    approval_resolver=approval_resolver,
                    on_wait_start=on_wait_start,
                    on_admitted=on_admitted,
                )
            )
        return results

    async def _execute_batch_item(
        self,
        query: StructuredQuery,
        *,
        queue_mode: Optional[QueueMode],
        wait_timeout_seconds: Optional[float],
        approval_tokens: Optional[Dict[str, str]],
        approval_resolver: Optional[ApprovalResolver],
        on_wait_start: Optional[Callable[[float], Awaitable[None]]] = None,
        on_admitted: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> BatchQueryItemResult:
        token = approval_tokens.get(query_fingerprint(query)) if approval_tokens else None
        try:
            result = await self.execute(
                query,
                queue_mode=queue_mode,
                wait_timeout_seconds=wait_timeout_seconds,
                approval_token=token,
                on_wait_start=on_wait_start,
                on_admitted=on_admitted,
            )
            return BatchQueryItemResult(**result.model_dump())
        except ApprovalRequiredError as exc:
            # No pre-supplied token covered this query (or it was rejected). Give
            # an injected resolver (MCP elicitation) one chance to obtain a token
            # interactively, then retry exactly once with it.
            if approval_resolver is not None:
                resolved = await approval_resolver(query, exc)
                if resolved is not None:
                    try:
                        result = await self.execute(
                            query,
                            queue_mode=queue_mode,
                            wait_timeout_seconds=wait_timeout_seconds,
                            approval_token=resolved,
                            # Reuse the reservation the first (paused) attempt
                            # already made — don't double-count quota (item 107).
                            _reserved_quota=exc.quota_reservation,  # type: ignore[arg-type]
                        )
                        return BatchQueryItemResult(**result.model_dump())
                    except Exception as retry_exc:  # shaped into the item error below
                        exc = retry_exc  # type: ignore[assignment]
            return self._batch_error_item(exc)
        except Exception as exc:
            return self._batch_error_item(exc)

    @staticmethod
    def _batch_error_item(exc: Exception) -> BatchQueryItemResult:
        return BatchQueryItemResult(
            error=public_error_message(exc),
            admission_id=getattr(exc, "admission_id", None),
            admission_state=getattr(exc, "admission_state", None),
            queue_wait_ms=getattr(exc, "queue_wait_ms", None),
            approval_fingerprint=(
                exc.fingerprint if isinstance(exc, ApprovalRequiredError) else None
            ),
            approval_reasons=(exc.reasons if isinstance(exc, ApprovalRequiredError) else None),
        )

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
            self._connection_id,
            policy.max_concurrency,
            policy.concurrency_wait_seconds,
            principal_subject=self._principal_subject,
            max_queue_depth=policy.max_queue_depth,
            max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
        ):
            stmt, limit, tables, _dialect, _policy, _scope_connections = (
                await self._validate_and_compile(query)
            )
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
    async def verdict(self, query: StructuredQuery) -> VerdictResult:
        """Report whether `query` would be allowed for this principal, without
        executing it or (by default) revealing a compiled plan (TODO.md item
        133 — the caller-facing counterpart to `explain`, for MCP gateways,
        proxies, and CI checks that need "may I run this" without a database
        round trip or debug-level detail).

        Unlike `explain`, this is quota-metered and audited unconditionally
        (never silently skipped, the same posture `execute` takes — though
        the audit event omits execution-only fields like row/byte counts and
        admission timing, since no rows are ever returned): it still
        reflects the schema (a cold-cache reflection is a real DB round
        trip), so leaving it free would make it strictly cheaper to abuse
        than a real query for probing policy/schema shape.

        A denial always reports `reason="not-available-to-you"` — it never
        distinguishes "on your policy's deny list" from "doesn't exist in the
        schema" from "references a join connection you can't see" from any
        other shape-level rejection reason. The inner `except Exception`
        below is deliberately a catch-all, not an allow-list of specific
        exception types: an earlier version caught only
        `(PolicyViolationError, QueryValidationError)`, then widened to add
        `NotFoundError`/`sa.exc.NoSuchTableError` after this item's own audit
        found each missing in turn — a pattern of type-by-type patching that
        had already missed twice. `_get_policy`/`enforce_query_quota`/
        `concurrency_slot` all run strictly before this inner block (in the
        outer `try`), so nothing reaching it is a quota/concurrency system
        failure — only `_validate_and_compile`'s validation/compilation/
        reflection failures land here, and every one of those is a "this
        query's shape isn't allowed" reason, never a "the system is busy"
        one. Catching broadly here is therefore fail-closed, not a
        weakening: `validate_policy` runs strictly before `validate_schema`,
        so returning any exception's real message (or even just which
        validator raised) would let a caller enumerate identifiers and
        reconstruct both the schema and the policy boundary one query at a
        time — the same oracle class `docs/THREAT_MODEL.md` QG-19/QG-24
        already close on the admin simulation and "my access" surfaces.
        `help/personal_denials.py` excludes `operation="query_verdict"`
        events from the caller-facing denial history for the same reason —
        a caller reading back their own already-submitted verdict probe's
        category would reopen exactly the channel this collapse closes.

        A quota or concurrency failure is a different kind of thing — "the
        system couldn't answer right now", not a verdict about the query's
        shape — so it is audited (for operator visibility) and then
        re-raised as a real error via the outer `except`, mirroring
        `execute`'s outer handler, rather than being folded into the generic
        denial.

        `explain` is deliberately left as-is (still echoes the real
        validation message) — it is an authenticated debugging tool a caller
        already needs identifier-level detail from, a different posture than
        this surface's "may I" question; tightening `explain` too was
        considered and rejected as unrelated scope creep for this item
        (recorded in the PRODUCT_GUIDE Decision Log). Note this means
        `explain` is not a privilege boundary either side of `verdict`: the
        same principal that can call `verdict` can call `explain` and get
        the real message, since QueryGate has no scope today that
        distinguishes "may see a verdict" from "may see debug detail" —
        `verdict`'s guarantee is response-shape hygiene for a caller that
        only ever calls this endpoint, not confinement against a caller that
        also has `explain`/`query` access (see docs/THREAT_MODEL.md QG-34's
        residual).
        """
        start = time.monotonic()
        query_shape = normalize_query_shape(query)
        sql = ""
        # None until `_get_policy()` succeeds below — see `execute()`'s
        # identical guard; the outer `except` must not assume it's bound.
        policy: Optional[Policy] = None
        try:
            policy = self._get_policy()
            # Reserving the unit is the point (this is the quota-metering
            # itself); there's no response body to attribute bytes to
            # afterward, unlike `execute`'s `record_query_quota_bytes` call.
            await enforce_query_quota(
                policy, connection_id=self._connection_id, principal_subject=self._principal_subject
            )
            async with concurrency_slot(
                self._connection_id,
                policy.max_concurrency,
                policy.concurrency_wait_seconds,
                principal_subject=self._principal_subject,
                max_queue_depth=policy.max_queue_depth,
                max_queue_depth_per_principal=policy.max_queue_depth_per_principal,
            ):
                try:
                    stmt, _limit, tables, _dialect, _policy, _scope_connections = (
                        await self._validate_and_compile(query)
                    )
                except Exception as exc:
                    audit_query(
                        connection_id=self._connection_id,
                        sql=sql,
                        intent=query.intent,
                        purpose=(
                            query.purpose
                            if policy is not None and policy.allowed_purposes
                            else None
                        ),
                        duration_ms=int((time.monotonic() - start) * 1000),
                        principal=self._principal_subject,
                        principal_scopes=self._principal_scopes,
                        actor=self._principal_actor,
                        delegation_chain=self._delegation_chain,
                        auth_method=self._auth_method,
                        surface=self._surface,
                        query_shape=query_shape,
                        error_category=(
                            "not_found"
                            if isinstance(exc, NotFoundError)
                            else classify_rejection(exc)
                        ),
                        policy_decision="denied",
                        rejected=True,
                        rejection_reason=str(exc),
                        operation="query_verdict",
                    )
                    VERDICTS_TOTAL.labels(connection=self._connection_id, outcome="denied").inc()
                    VERDICT_DURATION_SECONDS.labels(connection=self._connection_id).observe(
                        time.monotonic() - start
                    )
                    return VerdictResult(
                        allowed=False,
                        reason="not-available-to-you",
                        message="This query is not available to you under your effective policy.",
                    )
            # Outside the concurrency slot (compiling the plan text is pure
            # CPU work, not a DB round trip — no reason to hold the slot for
            # it) but still inside the outer `try`: a `_compile_to_text`
            # failure or a failure in the audit call itself must still be
            # audited (and reported as a real error, not a false "denied")
            # rather than silently 500ing with a quota unit already spent
            # and no trace left behind.
            plan = None
            if policy.verdict_include_plan:
                sql, _params = _compile_to_text(stmt, include_literals=False)
                plan = VerdictPlan(sql=sql, tables=sorted(tables))
            audit_query(
                connection_id=self._connection_id,
                sql=sql,
                intent=query.intent,
                purpose=(query.purpose if policy is not None and policy.allowed_purposes else None),
                duration_ms=int((time.monotonic() - start) * 1000),
                principal=self._principal_subject,
                principal_scopes=self._principal_scopes,
                actor=self._principal_actor,
                delegation_chain=self._delegation_chain,
                auth_method=self._auth_method,
                surface=self._surface,
                query_shape=query_shape,
                policy_decision="allowed",
                operation="query_verdict",
            )
            VERDICTS_TOTAL.labels(connection=self._connection_id, outcome="allowed").inc()
            VERDICT_DURATION_SECONDS.labels(connection=self._connection_id).observe(
                time.monotonic() - start
            )
            return VerdictResult(allowed=True, plan=plan)
        except Exception as exc:
            audit_query(
                connection_id=self._connection_id,
                sql=sql,
                intent=query.intent,
                purpose=(query.purpose if policy is not None and policy.allowed_purposes else None),
                duration_ms=int((time.monotonic() - start) * 1000),
                principal=self._principal_subject,
                principal_scopes=self._principal_scopes,
                actor=self._principal_actor,
                delegation_chain=self._delegation_chain,
                auth_method=self._auth_method,
                surface=self._surface,
                query_shape=query_shape,
                error_category=(
                    "not_found" if isinstance(exc, NotFoundError) else classify_rejection(exc)
                ),
                rejected=True,
                rejection_reason=str(exc),
                operation="query_verdict",
            )
            # A quota/concurrency failure here is a system-busy state, not a
            # shape verdict (see the docstring above and QG-34's residual) —
            # it deliberately does not touch VERDICTS_TOTAL, but it still
            # spends the same per-principal quota budget execute() does, so
            # it must be visible on the same counter execute() reports
            # through, or an operator has no verdict-shaped signal for "why
            # is this agent throttled" (TODO.md item 144).
            if isinstance(exc, QuotaExceededError):
                QUERY_QUOTA_REJECTIONS_TOTAL.labels(
                    connection=self._connection_id, quota_kind=exc.quota_kind
                ).inc()
            raise

    async def verdict_many(self, queries: List[StructuredQuery]) -> List[BatchVerdictItemResult]:
        """Verdict each query independently; one failure doesn't drop the rest.

        A `QuotaExceededError`/concurrency failure is a "couldn't determine
        an answer right now" system state, not a verdict about the query's
        shape, so it lands in `error` (mirroring `explain_many`'s per-item
        error tolerance) rather than being reported as `allowed=False`.
        """
        results: List[BatchVerdictItemResult] = []
        for query in queries:
            try:
                result = await self.verdict(query)
                results.append(BatchVerdictItemResult(**result.model_dump()))
            except Exception as exc:
                results.append(BatchVerdictItemResult(error=public_error_message(exc)))
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
