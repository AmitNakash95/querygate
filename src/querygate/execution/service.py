"""Validate, compile, and execute structured reads against one connection.

This is the single seam every REST route and MCP tool goes through — there
is no other path to a database from agent/client-facing code.
"""

from __future__ import annotations

import json
import time
from typing import Any, List, Optional, Tuple

import pydantic as pyd
import sqlalchemy as sa

from querygate.audit.events import AuditSurface, normalize_query_shape
from querygate.audit.logger import audit_query
from querygate.catalog.loader import get_catalog_store
from querygate.catalog.models import (
    ColumnCatalogEntry,
    RelationshipHint,
    SensitivityClass,
    visible_relationships,
)
from querygate.compiler.sqlalchemy_compiler import compile_structured_query
from querygate.connections.engine import get_engine, get_metadata, session_scope
from querygate.connections.registry import get_registry
from querygate.connections.visibility import resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.exceptions import (
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
    public_error_message,
)
from querygate.core.logging import log_execution
from querygate.execution.concurrency import concurrency_slot
from querygate.execution.cost_estimation import enforce_cost_estimate, estimate_postgres_query_cost
from querygate.metrics import (
    QUERIES_REJECTED_TOTAL,
    QUERIES_TOTAL,
    QUERY_DURATION_SECONDS,
    classify_rejection,
)
from querygate.policy.models import Policy
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
    relationships: List[RelationshipHint] = pyd.Field(default_factory=list)


class ColumnInfo(pyd.BaseModel):
    name: str
    type: str
    nullable: bool
    description: Optional[str] = None
    catalog: Optional[ColumnCatalogEntry] = None


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
    explain_structured_query's response, which contradicts the "audit
    records never contain row payloads" guarantee for anything expressible
    in a `where`. `include_literals=True` is an explicit per-policy opt-in
    (`Policy.log_query_literals`) for deployments that want full literal SQL
    for debugging.
    """
    compiled = stmt.compile()
    params = dict(compiled.params) if hasattr(compiled, "params") else {}

    if include_literals:
        try:
            literal_compiled = stmt.compile(compile_kwargs={"literal_binds": True})
            return str(literal_compiled), None
        except Exception:
            pass  # some param types (e.g. arrays) can't render as literals — fall through

    redacted = {k: "<redacted>" for k in params}
    return str(compiled), (str(redacted) if redacted else None)


class StructuredQueryService:
    """Validate, compile, and execute structured reads against `connection_id`."""

    def __init__(
        self,
        connection_id: str,
        principal: Optional[Principal] = None,
        surface: AuditSurface = "internal",
    ) -> None:
        self._connection_id = connection_id
        self._principal = principal
        self._surface = surface

    @property
    def _principal_subject(self) -> Optional[str]:
        return self._principal.subject if self._principal else None

    @property
    def _principal_scopes(self) -> Optional[List[str]]:
        return sorted(self._principal.scopes) if self._principal else None

    @property
    def _auth_method(self) -> str:
        return self._principal.auth_method if self._principal else "unknown"

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

    @log_execution
    async def execute(self, query: StructuredQuery) -> StructuredQueryResult:
        start = time.monotonic()
        query_shape = normalize_query_shape(query)
        sql = ""
        params: Optional[str] = None
        policy_validated = False
        try:
            policy = self._get_policy()
            async with concurrency_slot(
                self._connection_id, policy.max_concurrency, policy.concurrency_wait_seconds
            ):
                stmt, limit, _tables, dialect = await self._validate_and_compile(query)
                policy_validated = True
                sql, params = _compile_to_text(stmt, include_literals=policy.log_query_literals)

                async with session_scope(self._connection_id, policy=policy) as session:
                    if policy.cost_estimation_enabled and dialect == "postgresql":
                        estimate = await estimate_postgres_query_cost(session, stmt)
                        if estimate is not None:
                            enforce_cost_estimate(estimate, policy)
                    result = await session.execute(stmt)
                    raw_rows = [dict(r) for r in result.mappings().all()]

                rows = [_clean_row_values(r) for r in raw_rows]
                row_limit_hit = len(rows) >= limit
                rows, byte_cap_hit = _cap_response_bytes(rows, policy.max_response_bytes)
                truncated = row_limit_hit or byte_cap_hit
                response_bytes = len(json.dumps(rows, default=str).encode("utf-8"))
                elapsed_seconds = time.monotonic() - start
                audit_query(
                    connection_id=self._connection_id,
                    sql=sql,
                    params=params,
                    intent=query.intent,
                    row_count=len(rows),
                    duration_ms=int(elapsed_seconds * 1000),
                    principal=self._principal_subject,
                    principal_scopes=self._principal_scopes,
                    auth_method=self._auth_method,
                    surface=self._surface,
                    query_shape=query_shape,
                    response_bytes=response_bytes,
                    truncated=truncated,
                    policy_decision="allowed",
                )
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
                )
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
                auth_method=self._auth_method,
                surface=self._surface,
                query_shape=query_shape,
                error_category=error_category,
                policy_decision=(
                    "allowed"
                    if policy_validated
                    else (
                        "denied"
                        if error_category in ("policy", "schema", "not_found")
                        else "unknown"
                    )
                ),
                rejected=True,
                rejection_reason=str(exc),
            )
            QUERIES_TOTAL.labels(connection=self._connection_id, status="rejected").inc()
            QUERIES_REJECTED_TOTAL.labels(
                connection=self._connection_id, reason=classify_rejection(exc)
            ).inc()
            raise

    async def execute_many(self, queries: List[StructuredQuery]) -> List[BatchQueryItemResult]:
        """Run each query independently; one failure doesn't drop the rest of the batch."""
        results: List[BatchQueryItemResult] = []
        for query in queries:
            try:
                result = await self.execute(query)
                results.append(BatchQueryItemResult(**result.model_dump()))
            except Exception as exc:
                results.append(BatchQueryItemResult(error=public_error_message(exc)))
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
    async def describe_table(self, table_name: str) -> TableDescription:
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
        catalog_entry = get_catalog_store().get_table(self._connection_id, table_name)
        columns = [
            ColumnInfo(
                name=col.name,
                type=_render_column_type(col.type),
                nullable=bool(col.nullable),
                description=col.comment,
                catalog=(catalog_entry.column(col.name) if catalog_entry else None),
            )
            for col in table.columns
            if policy.column_allowed(table_name, col.name)
        ]
        table_catalog = (
            TableCatalogInfo(
                description=catalog_entry.description,
                aliases=catalog_entry.aliases,
                sensitivity=catalog_entry.sensitivity,
                default_aggregation=catalog_entry.default_aggregation,
                allow_samples=catalog_entry.allow_samples,
                relationships=visible_relationships(catalog_entry, policy),
            )
            if catalog_entry
            else None
        )
        return TableDescription(
            name=table.name, columns=columns, description=table.comment, catalog=table_catalog
        )
