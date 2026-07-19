"""REST routes: connection discovery, schema discovery, and structured queries.

No raw-SQL endpoint exists anywhere in this router — every query is a
StructuredQuery AST, validated against schema + policy before compilation.
"""

from __future__ import annotations

from typing import Callable, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from querygate.config_reload import ReloadResult, reload_config
from querygate.connections.models import PublicConnectionInfo
from querygate.connections.visibility import list_visible_connections, resolve_visible_connection
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import (
    PUBLIC_INTERNAL_ERROR,
    CapacityTimeoutError,
    ConcurrencyLimitError,
    NotFoundError,
    PolicyViolationError,
    QueryValidationError,
)
from querygate.execution.admission import QueueMode
from querygate.execution.service import (
    BatchQueryItemResult,
    ExplainResult,
    StructuredQueryResult,
    StructuredQueryService,
    TableDescription,
)
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.core.scopes import ADMIN_RELOAD_CONFIG_SCOPE
from querygate.secrets.resolvers import build_secret_resolver_registry
from querygate.validation.policy_validation import validate_batch_size


class TablesListResult(pyd.BaseModel):
    tables: List[str]


class BatchQueryRequest(pyd.BaseModel):
    queries: List[StructuredQuery] = pyd.Field(min_length=1)


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


# Agent-visible admission info (TODO.md item 35 phase 1) is surfaced as
# response headers rather than in the JSON body, so the existing
# `{"detail": "too many concurrent ..."}` 422 shape callers already parse
# (see docs/LOAD_TESTING.md) never changes.
_ADMISSION_ID_HEADER = "X-QueryGate-Admission-Id"
_ADMISSION_STATE_HEADER = "X-QueryGate-Admission-State"
_QUEUE_WAIT_HEADER = "X-QueryGate-Queue-Wait-Ms"

_QUEUE_MODE_QUERY = Query(
    default=None,
    description=(
        "fail_fast: don't wait for a concurrency slot at all, reject immediately if the "
        "connection is at capacity. wait (default): wait up to wait_timeout_seconds, or the "
        "policy's own concurrency_wait_seconds ceiling if wait_timeout_seconds is omitted."
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


def _admission_headers(
    *, admission_id: Optional[str], state: str, queue_wait_ms: Optional[int]
) -> dict:
    headers = {_ADMISSION_STATE_HEADER: state}
    if admission_id is not None:
        headers[_ADMISSION_ID_HEADER] = admission_id
    if queue_wait_ms is not None:
        headers[_QUEUE_WAIT_HEADER] = str(queue_wait_ms)
    return headers


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
        try:
            tables = await service.list_tables()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=PUBLIC_INTERNAL_ERROR,
            )
        return TablesListResult(tables=tables)

    @router.get("/{connection}/tables/{table}", response_model=TableDescription)
    async def describe_table(
        connection: str, table: str, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            return await service.describe_table(table)
        except (PolicyViolationError, QueryValidationError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=PUBLIC_INTERNAL_ERROR,
            )

    @router.post("/{connection}/query/explain", response_model=ExplainResult)
    async def explain_query(
        connection: str, query: StructuredQuery, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            return await service.explain(query)
        except (PolicyViolationError, QueryValidationError, ConcurrencyLimitError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=PUBLIC_INTERNAL_ERROR,
            )

    @router.post("/{connection}/query", response_model=StructuredQueryResult)
    async def execute_query(
        connection: str,
        query: StructuredQuery,
        response: Response,
        principal: Principal = Depends(get_principal),
        queue_mode: Optional[QueueMode] = _QUEUE_MODE_QUERY,
        wait_timeout_seconds: Optional[float] = _WAIT_TIMEOUT_QUERY,
    ):
        service = _service(connection, principal)
        try:
            result = await service.execute(
                query, queue_mode=queue_mode, wait_timeout_seconds=wait_timeout_seconds
            )
        except CapacityTimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
                headers=_admission_headers(
                    admission_id=exc.admission_id,
                    state="capacity_timeout",
                    queue_wait_ms=exc.queue_wait_ms,
                ),
            )
        except (PolicyViolationError, QueryValidationError, ConcurrencyLimitError) as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=PUBLIC_INTERNAL_ERROR,
            )
        response.headers.update(
            _admission_headers(
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
        service = _service(connection, principal)
        try:
            validate_batch_size(len(payload.queries), get_policy(connection, principal=principal))
        except PolicyViolationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        results = await service.execute_many(
            payload.queries, queue_mode=queue_mode, wait_timeout_seconds=wait_timeout_seconds
        )
        return BatchQueryResult(results=results)

    @router.post("/admin/reload-config", response_model=ReloadResult)
    async def reload_config_endpoint(principal: Principal = Depends(get_principal)):
        if ADMIN_RELOAD_CONFIG_SCOPE not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {ADMIN_RELOAD_CONFIG_SCOPE!r}",
            )
        try:
            return await reload_config(
                connections_file=cfg.connections_file,
                policy_file=cfg.policy_file,
                catalog_file=cfg.catalog_file,
                resolver_registry=build_secret_resolver_registry(cfg),
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return router
