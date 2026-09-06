"""REST routes: connection discovery, schema discovery, and structured queries.

No raw-SQL endpoint exists anywhere in this router — every query is a
StructuredQuery AST, validated against schema + policy before compilation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Callable, List, Literal, Optional, Union

import pydantic as pyd
import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from querygate.admin.service import safe_pydantic_error_lines, safe_yaml_error_detail
from querygate.api._errors import admission_headers, mask_unexpected, require_scope
from querygate.config_reload import ReloadResult, reload_config
from querygate.catalog.retrieval import CatalogSearchResponse
from querygate.connections.engine import get_engine
from querygate.connections.models import PublicConnectionInfo
from querygate.connections.visibility import list_visible_connections, resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, config as app_config
from querygate.core.exceptions import (
    NotFoundError,
    QueryCancellationNotEnabledError,
    QueryCancellationNotReadyError,
    QueryValidationError,
)
from querygate.execution.admission import QueueMode, reject_unsupported_async_queue_mode
from querygate.execution.async_execution import (
    AsyncExecutionRecord,
    async_execution_store,
    request_cancel,
    start_async_execution,
)
from querygate.execution.approval import (
    issue_approval_token,
    query_fingerprint,
    write_fingerprint,
)
from querygate.execution.write_execution import WriteExecutionService, WriteResult
from querygate.execution.write_preview import WritePreview, WritePreviewService
from querygate.write_ast.models import (
    DeleteStatement,
    InsertStatement,
    UpdateStatement,
    UpsertStatement,
)
from querygate.execution.service import (
    BatchQueryItemResult,
    ExplainResult,
    StructuredQueryResult,
    StructuredQueryService,
    TableDescription,
    VerdictResult,
)
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.core.scopes import ADMIN_RELOAD_CONFIG_SCOPE, QUERY_APPROVE_SCOPE, QUERY_CANCEL_SCOPE
from querygate.secrets.resolvers import build_secret_resolver_registry
from querygate.templates.binding import bind_template
from querygate.templates.loader import get_template_store
from querygate.templates.models import PublicQueryTemplate, QueryTemplate
from querygate.validation.policy_validation import validate_batch_size

from typing import Any, Dict


class TablesListResult(pyd.BaseModel):
    tables: List[str]


class TemplateRunRequest(pyd.BaseModel):
    parameters: Dict[str, Any] = pyd.Field(default_factory=dict)


class ApprovalGrant(pyd.BaseModel):
    """A granted in-query approval (item 92): the fingerprint of the approved
    query and the signed token to re-submit it with. No query values, no
    secret — the token is an opaque HMAC over the fingerprint + expiry."""

    fingerprint: str
    approval_token: str


class AsyncQueryAdmission(pyd.BaseModel):
    """`202` body for `queue_mode=async` (TODO.md item 35 phase 3): the
    `admission_id` to poll/cancel, and the URL to do so at."""

    admission_id: str
    status_url: str


class AsyncQueryStatus(pyd.BaseModel):
    """`GET .../query/{admission_id}` response. `result`/`error` are set only
    once `state` reaches a terminal value (`completed`/`failed`/`cancelled`)."""

    admission_id: str
    state: str
    queue_wait_ms: Optional[int] = None
    result: Optional[StructuredQueryResult] = None
    error: Optional[str] = None
    admission_state: Optional[str] = None


class AsyncQueryCancelResult(pyd.BaseModel):
    admission_id: str
    state: str


def _visible_template(connection_id: str, principal: Principal) -> None:
    """A template is visible/invocable only if its target connection is —
    reuse the exact non-enumerating connection-visibility rule (item 22), and
    report an invisible template's connection as an unknown template so it
    can't be used to probe hidden connection ids.
    """
    resolve_visible_connection(connection_id, principal=principal)


class BatchQueryRequest(pyd.BaseModel):
    queries: List[StructuredQuery] = pyd.Field(min_length=1)
    approval_tokens: Dict[str, str] = pyd.Field(
        default_factory=dict,
        description=(
            "In-query approval grants for this batch (item 92): a map of query "
            "fingerprint -> the signed token from POST /query/approve. A query "
            "that trips the approval gate without a matching token fails only "
            "that batch item (fail-closed); each token is verified against its "
            "own query's fingerprint, so it can't be replayed onto another."
        ),
    )


class BatchQueryResult(pyd.BaseModel):
    results: List[BatchQueryItemResult]


def _require_connection(connection_id: str, principal: Principal) -> None:
    try:
        resolve_visible_connection(connection_id, principal=principal)
    except NotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown connection: {connection_id!r}"
        )


def _service(connection_id: str, principal: Principal) -> StructuredQueryService:
    _require_connection(connection_id, principal)
    return StructuredQueryService(connection_id=connection_id, principal=principal, surface="rest")


def _require_async_record(
    connection_id: str, admission_id: str, principal: Principal
) -> AsyncExecutionRecord:
    """Look up an async execution record, 404-ing uniformly whether the id is
    unknown, belongs to a different connection, or (see the scope check at
    each call site) belongs to a different principal — never an enumeration
    oracle, the same posture `_require_connection` already uses."""
    _require_connection(connection_id, principal)
    record = async_execution_store().get(admission_id)
    if record is None or record.connection_id != connection_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown async query admission: {admission_id!r}",
        )
    if record.principal_subject != principal.subject and QUERY_CANCEL_SCOPE not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown async query admission: {admission_id!r}",
        )
    return record


_QUEUE_MODE_QUERY = Query(
    default=None,
    description=(
        "fail_fast: don't wait for a concurrency slot at all, reject immediately if the "
        "connection is at capacity. wait (default): wait up to wait_timeout_seconds, or the "
        "policy's own concurrency_wait_seconds ceiling if wait_timeout_seconds is omitted. "
        "async: return 202 immediately with an admission_id instead of blocking, polled via "
        "GET .../query/{admission_id} — only honored by POST /{connection}/query "
        "(single-query execution); rejected as a validation error on every other endpoint "
        "that accepts this parameter."
    ),
)
_WAIT_TIMEOUT_QUERY = Query(
    default=None,
    ge=0,
    description=(
        "Caller-requested wait (seconds) for a concurrency slot. Clamped to the operator's "
        "policy concurrency_wait_seconds ceiling — a caller may request a shorter wait, "
        "never a longer one."
    ),
)


def build_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.get("/connections", response_model=List[PublicConnectionInfo])
    async def list_connections(principal: Principal = Depends(get_principal)):
        return list_visible_connections(principal)

    @router.get("/{connection}/tables", response_model=TablesListResult)
    async def list_tables(connection: str, principal: Principal = Depends(get_principal)):
        service = _service(connection, principal)
        with mask_unexpected():
            tables = await service.list_tables()
        return TablesListResult(tables=tables)

    @router.get("/{connection}/tables/{table}", response_model=TableDescription)
    async def describe_table(
        connection: str,
        table: str,
        verbose_provenance: bool = Query(
            default=False,
            description=(
                "False (default): each catalog citation is compact (status + precedence "
                "only). True: full citation (entry id, evidence, confidence, catalog/schema "
                "version, freshness)."
            ),
        ),
        principal: Principal = Depends(get_principal),
    ):
        service = _service(connection, principal)
        with mask_unexpected():
            return await service.describe_table(table, verbose_provenance=verbose_provenance)

    @router.get("/{connection}/catalog/search", response_model=CatalogSearchResponse)
    async def search_catalog(
        connection: str,
        q: str = Query(min_length=1, max_length=256),
        limit: int = Query(default=5, ge=1, le=20),
        verbose_provenance: bool = Query(
            default=False,
            description=(
                "False (default): each hit's citation is compact (status + precedence "
                "only). True: full citation (entry id, evidence, confidence, catalog/schema "
                "version, freshness)."
            ),
        ),
        principal: Principal = Depends(get_principal),
    ):
        service = _service(connection, principal)
        with mask_unexpected():
            return await service.search_catalog(
                q, max_results=limit, verbose_provenance=verbose_provenance
            )

    @router.post("/{connection}/query/explain", response_model=ExplainResult)
    async def explain_query(
        connection: str, query: StructuredQuery, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        with mask_unexpected():
            return await service.explain(query)

    @router.post("/{connection}/query/verdict", response_model=VerdictResult)
    async def query_verdict(
        connection: str, query: StructuredQuery, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        with mask_unexpected():
            return await service.verdict(query)

    @router.post(
        "/{connection}/query", response_model=Union[StructuredQueryResult, AsyncQueryAdmission]
    )
    async def execute_query(
        connection: str,
        query: StructuredQuery,
        response: Response,
        principal: Principal = Depends(get_principal),
        queue_mode: Optional[QueueMode] = _QUEUE_MODE_QUERY,
        wait_timeout_seconds: Optional[float] = _WAIT_TIMEOUT_QUERY,
        approval_token: Optional[str] = Header(default=None, alias="X-QueryGate-Approval"),
    ):
        service = _service(connection, principal)
        if queue_mode == QueueMode.ASYNC:
            # The background task always waits/executes exactly like `wait`
            # would — `async` only changes whether THIS caller blocks on the
            # HTTP response, never the wait-ceiling semantics `resolve_wait_seconds`
            # already enforces identically for both (see admission.py).
            async def _execute_coro(admission_id, on_admitted, on_session_identifier):
                return await service.execute(
                    query,
                    wait_timeout_seconds=wait_timeout_seconds,
                    approval_token=approval_token,
                    _admission_id=admission_id,
                    on_admitted=on_admitted,
                    on_session_identifier=on_session_identifier,
                )

            record = start_async_execution(connection, principal.subject, _execute_coro)
            response.status_code = status.HTTP_202_ACCEPTED
            return AsyncQueryAdmission(
                admission_id=record.admission_id,
                status_url=f"/api/v1/{connection}/query/{record.admission_id}",
            )
        with mask_unexpected():
            # An ApprovalRequiredError propagates to the 428 handler in _errors.py
            # carrying the fingerprint + reasons; the caller gets a token from
            # /query/approve and re-submits with the X-QueryGate-Approval header.
            result = await service.execute(
                query,
                queue_mode=queue_mode,
                wait_timeout_seconds=wait_timeout_seconds,
                approval_token=approval_token,
            )
        response.headers.update(
            admission_headers(
                admission_id=result.admission_id,
                state="completed",
                queue_wait_ms=result.queue_wait_ms,
            )
        )
        return result

    @router.get("/{connection}/query/{admission_id}", response_model=AsyncQueryStatus)
    async def get_query_status(
        connection: str,
        admission_id: str,
        principal: Principal = Depends(get_principal),
    ):
        """Poll a `queue_mode=async` execution (TODO.md item 35 phase 3). Only
        the submitting principal or a caller holding `query:cancel` may view
        it — a `404`, not a `403`, for anyone else, so the admission_id space
        can't be used to probe which ids exist."""
        record = _require_async_record(connection, admission_id, principal)
        return AsyncQueryStatus(
            admission_id=record.admission_id,
            state=record.state,
            queue_wait_ms=record.queue_wait_ms,
            result=record.result,
            error=record.error_message,
            admission_state=record.error_admission_state,
        )

    @router.post("/{connection}/query/{admission_id}/cancel", response_model=AsyncQueryCancelResult)
    async def cancel_query(
        connection: str,
        admission_id: str,
        principal: Principal = Depends(get_principal),
    ):
        """Request cancellation of a `queue_mode=async` execution (TODO.md
        item 35 phase 3). Cancelling your own query needs no scope; cancelling
        another principal's needs `query:cancel`. Idempotent — cancelling an
        already-terminal execution is a no-op that returns its current state.
        A RUNNING query can only be cancelled for real if the connection's
        policy has `allow_query_cancellation` set (`403` otherwise) — rejected
        before any DB call, never a permission error discovered mid-cancellation.
        """
        record = _require_async_record(connection, admission_id, principal)
        if record.principal_subject != principal.subject:
            require_scope(principal, QUERY_CANCEL_SCOPE)
        profile, policy = resolve_visible_connection(connection, principal=principal)
        with mask_unexpected():
            # An unexpected failure from cancel_session itself (e.g. a real
            # Postgres/MSSQL permission error if an operator enabled
            # allow_query_cancellation without actually granting the DB-level
            # permission yet) must not leak driver text past this boundary,
            # same as every other DB-touching route in this file.
            try:
                state = await request_cancel(
                    record,
                    allow_query_cancellation=policy.allow_query_cancellation,
                    engine=get_engine(connection),
                    dialect=profile.dialect,
                )
            except QueryCancellationNotEnabledError as exc:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
            except QueryCancellationNotReadyError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        return AsyncQueryCancelResult(admission_id=record.admission_id, state=state)

    @router.post("/{connection}/query/approve", response_model=ApprovalGrant)
    async def approve_query(
        connection: str,
        query: StructuredQuery,
        principal: Principal = Depends(get_principal),
    ):
        """Grant an approval token for a query that tripped the in-query
        human-in-the-loop gate (TODO.md item 92). Requires the `query:approve`
        scope — deliberately a distinct scope from query execution, so a
        principal that only ever holds an execution-capable scope can never
        mint its own approval. Returns a short-lived, HMAC-signed token bound
        to this exact query's fingerprint, connection, and this call's own
        principal (TODO.md item 151); **that same principal** — not a
        different requester — is the only one who can redeem it, by
        re-submitting the identical query against this same connection with it
        in the `X-QueryGate-Approval` header. This means the only configuration
        that can self-approve is one principal deliberately granted both
        `query:approve` and execution scope — a deployment choice, not a gap
        this endpoint can close on its own. Stateless: no approval is stored
        server-side.
        """
        require_scope(principal, QUERY_APPROVE_SCOPE)
        _require_connection(connection, principal)
        # The approval HMAC key is a process-level secret (like the audit-ledger
        # key): read from the shared config singleton, the same object the
        # per-request service reads when it VERIFIES the token, so the issue and
        # verify sides can never diverge on the key.
        if not app_config.approval_token_hmac_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval grants are not configured (APPROVAL_TOKEN_HMAC_KEY is unset).",
            )
        fingerprint = query_fingerprint(query)
        token = issue_approval_token(
            fingerprint=fingerprint,
            approver_subject=principal.subject,
            key=app_config.approval_token_hmac_key,
            connection_id=connection,
            principal_subject=principal.subject,
        )
        return ApprovalGrant(fingerprint=fingerprint, approval_token=token)

    @router.post("/{connection}/write/preview", response_model=WritePreview)
    async def preview_write(
        connection: str,
        statement: Annotated[
            Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement],
            pyd.Field(discriminator="op"),
        ],
        principal: Principal = Depends(get_principal),
        include_diff: bool = Query(
            default=False,
            description=(
                "Also return the bounded old→new row diff of exactly what this "
                "write would change (item 93 phase 2b) — computed by running the "
                "DML in a rolled-back transaction. Masked columns are redacted; "
                "the number of rows shown is capped by WritePolicy.max_diff_rows."
            ),
        ),
    ):
        """Governed-writes dry-run preview (TODO.md item 93): validate a proposed
        INSERT/UPDATE/DELETE against WritePolicy + schema, compile it, and report
        the affected-row count + parameterized SQL (and, with `include_diff`, the
        bounded old→new diff of exactly what would change). **Nothing is ever
        executed or committed** — the diff runs the DML only inside a rolled-back
        transaction. Gated by WritePolicy (deny-by-default): a read-only
        deployment returns a clean policy rejection."""
        _require_connection(connection, principal)
        service = WritePreviewService(connection_id=connection, principal=principal)
        with mask_unexpected():
            return await service.preview(statement, include_diff=include_diff)

    @router.post("/{connection}/write/execute", response_model=WriteResult)
    async def execute_write(
        connection: str,
        statement: Annotated[
            Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement],
            pyd.Field(discriminator="op"),
        ],
        principal: Principal = Depends(get_principal),
        approval_token: Optional[str] = Header(default=None, alias="X-QueryGate-Approval"),
    ):
        """Governed-writes gated execution (TODO.md item 93 phase 2): validate an
        INSERT/UPDATE/DELETE against WritePolicy + schema, compile it, and — only
        if in policy and within the affected-row cap — commit it in a single
        transaction. Deny-by-default (a read-only deployment returns a clean
        policy rejection); no raw DML path exists. A write over the policy's
        `require_approval_over_rows` returns **428** with the write `fingerprint`
        (sign it at `POST /{connection}/write/approve`, resubmit with the
        `X-QueryGate-Approval` header). The affected-row cap is re-checked inside
        the transaction, so a race can't over-write; any error rolls the whole
        write back — never a partial mutation."""
        _require_connection(connection, principal)
        service = WriteExecutionService(
            connection_id=connection, principal=principal, surface="rest"
        )
        with mask_unexpected():
            # An ApprovalRequiredError propagates to the 428 handler in _errors.py
            # carrying the write fingerprint + reasons.
            return await service.execute(statement, approval_token=approval_token)

    @router.post("/{connection}/write/approve", response_model=ApprovalGrant)
    async def approve_write(
        connection: str,
        statement: Annotated[
            Union[InsertStatement, UpdateStatement, DeleteStatement, UpsertStatement],
            pyd.Field(discriminator="op"),
        ],
        principal: Principal = Depends(get_principal),
    ):
        """Grant an approval token for a write that tripped the governed-writes
        approval gate (TODO.md item 93 phase 2). Requires the `query:approve`
        scope — the same scope split as read approval, so a principal that
        only ever holds an execution-capable scope can never mint its own
        approval. Returns a short-lived, HMAC-signed token bound to this exact
        write's fingerprint, connection, and this call's own principal
        (TODO.md item 151); **that same principal** redeems it by resubmitting
        the identical write against this same connection with it in the
        `X-QueryGate-Approval` header. The only configuration that can
        self-approve is one principal deliberately granted both
        `query:approve` and execution scope — a deployment choice, not a gap
        this endpoint can close on its own. Stateless: no approval is stored
        server-side."""
        require_scope(principal, QUERY_APPROVE_SCOPE)
        _require_connection(connection, principal)
        if not app_config.approval_token_hmac_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Approval grants are not configured (APPROVAL_TOKEN_HMAC_KEY is unset).",
            )
        fingerprint = write_fingerprint(statement)
        token = issue_approval_token(
            fingerprint=fingerprint,
            approver_subject=principal.subject,
            key=app_config.approval_token_hmac_key,
            connection_id=connection,
            principal_subject=principal.subject,
        )
        return ApprovalGrant(fingerprint=fingerprint, approval_token=token)

    @router.get("/query-templates", response_model=List[PublicQueryTemplate])
    async def list_query_templates(principal: Principal = Depends(get_principal)):
        """Curated query templates whose target connection is visible to the
        caller — the finite set of named, parameterized queries this principal
        may invoke (TODO.md item 48).
        """
        visible: List[PublicQueryTemplate] = []
        for template in get_template_store().list():
            try:
                _visible_template(template.connection, principal)
            except NotFoundError:
                continue
            visible.append(PublicQueryTemplate.from_template(template))
        return visible

    @router.post("/query-templates/{template_id}/run", response_model=StructuredQueryResult)
    async def run_query_template(
        template_id: str,
        request: TemplateRunRequest,
        response: Response,
        principal: Principal = Depends(get_principal),
        queue_mode: Optional[QueueMode] = _QUEUE_MODE_QUERY,
        wait_timeout_seconds: Optional[float] = _WAIT_TIMEOUT_QUERY,
    ):
        reject_unsupported_async_queue_mode(queue_mode)
        template: Optional[QueryTemplate] = get_template_store().get(template_id)
        # Uniform not-found whether the template is unknown or its connection is
        # hidden from this principal — never an enumeration oracle.
        if template is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown query template: {template_id!r}",
            )
        try:
            _visible_template(template.connection, principal)
        except NotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown query template: {template_id!r}",
            )
        service = StructuredQueryService(
            connection_id=template.connection,
            principal=principal,
            surface="rest",
            template_id=template.id,
            template_param_shape=sorted(request.parameters),
        )
        # Bind + execute exactly like execute_query: parameter binding and the
        # pipeline raise the same actionable domain exceptions, which the
        # centralized app-level handlers map (CapacityTimeoutError/
        # ConcurrencyLimitError -> 429 with admission headers/Retry-After,
        # PolicyViolationError/QueryValidationError -> 422); mask_unexpected
        # masks everything else.
        with mask_unexpected():
            query = bind_template(template, request.parameters)
            result = await service.execute(
                query, queue_mode=queue_mode, wait_timeout_seconds=wait_timeout_seconds
            )
        response.headers.update(
            admission_headers(
                admission_id=result.admission_id,
                state="completed",
                queue_wait_ms=result.queue_wait_ms,
            )
        )
        return result

    @router.post("/{connection}/query/batch", response_model=BatchQueryResult)
    async def execute_query_batch(
        connection: str,
        payload: BatchQueryRequest,
        principal: Principal = Depends(get_principal),
        queue_mode: Optional[QueueMode] = _QUEUE_MODE_QUERY,
        wait_timeout_seconds: Optional[float] = _WAIT_TIMEOUT_QUERY,
    ):
        reject_unsupported_async_queue_mode(queue_mode)
        service = _service(connection, principal)
        validate_batch_size(len(payload.queries), get_policy(connection, principal=principal))
        results = await service.execute_many(
            payload.queries,
            queue_mode=queue_mode,
            wait_timeout_seconds=wait_timeout_seconds,
            approval_tokens=payload.approval_tokens,
        )
        return BatchQueryResult(results=results)

    @router.post("/admin/reload-config", response_model=ReloadResult)
    async def reload_config_endpoint(principal: Principal = Depends(get_principal)):
        require_scope(principal, ADMIN_RELOAD_CONFIG_SCOPE)
        try:
            return await reload_config(
                connections_file=cfg.connections_file,
                policy_file=cfg.policy_file,
                catalog_file=cfg.catalog_file,
                template_file=cfg.template_file,
                identity_file=cfg.identity_file if cfg.sso_enabled else None,
                local_users_file=cfg.local_users_file if cfg.local_idp_enabled else None,
                resolver_registry=build_secret_resolver_registry(cfg),
            )
        except pyd.ValidationError as exc:
            # A malformed connections.yaml entry is validated by
            # ConnectionProfile.model_validate() *after* ${...} interpolation,
            # so the object pydantic is validating already carries a live
            # credential. str(exc) (and error["input"]/["input_value"]) can
            # embed that whole object for some error kinds (e.g. a
            # `missing`-type error on a required field) -- never surface the
            # raw exception here. Build detail from loc/msg only. See item 165.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="; ".join(safe_pydantic_error_lines(exc)),
            )
        except yaml.YAMLError as exc:
            # A syntactically-broken connections.yaml (e.g. an unterminated
            # quote on a connection_string: line) raises before pydantic
            # ever runs, on the SAME already-interpolated content -- so the
            # line PyYAML points at can itself contain the live credential.
            # str(exc) calls Mark.__str__(), which embeds that source line
            # verbatim; never surface it. See item 165.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=safe_yaml_error_detail(exc),
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return router
