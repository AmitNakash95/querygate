"""REST routes: connection discovery, schema discovery, and structured queries.

No raw-SQL endpoint exists anywhere in this router — every query is a
StructuredQuery AST, validated against schema + policy before compilation.
"""

from __future__ import annotations

from typing import Callable, List

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, status

from querygate.config_reload import ReloadResult, reload_config
from querygate.connections.models import PublicConnectionInfo
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.exceptions import PolicyViolationError
from querygate.execution.service import (
    BatchQueryItemResult,
    ExplainResult,
    StructuredQueryResult,
    StructuredQueryService,
    TableDescription,
)
from querygate.policy.loader import get_policy
from querygate.query_ast.models import StructuredQuery
from querygate.validation.policy_validation import validate_batch_size

# Scope required to call POST /admin/reload-config — reloading connections/
# policy is sensitive enough (it can swap which databases and rules are
# active) to gate behind an explicit grant rather than any authenticated
# caller, unlike the read-only/query endpoints below.
ADMIN_RELOAD_CONFIG_SCOPE = "admin:reload-config"


class TablesListResult(pyd.BaseModel):
    tables: List[str]


class BatchQueryRequest(pyd.BaseModel):
    queries: List[StructuredQuery] = pyd.Field(min_length=1)


class BatchQueryResult(pyd.BaseModel):
    results: List[BatchQueryItemResult]


def _require_connection(connection_id: str) -> None:
    try:
        get_registry().get(connection_id)
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown connection: {connection_id!r}"
        )


def _service(connection_id: str, principal: Principal) -> StructuredQueryService:
    _require_connection(connection_id)
    return StructuredQueryService(connection_id=connection_id, principal=principal)


def build_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    @router.get("/connections", response_model=List[PublicConnectionInfo])
    async def list_connections(principal: Principal = Depends(get_principal)):
        return get_registry().list_public()

    @router.get("/{connection}/tables", response_model=TablesListResult)
    async def list_tables(connection: str, principal: Principal = Depends(get_principal)):
        service = _service(connection, principal)
        try:
            tables = await service.list_tables()
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
        return TablesListResult(tables=tables)

    @router.get("/{connection}/tables/{table}", response_model=TableDescription)
    async def describe_table(
        connection: str, table: str, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            return await service.describe_table(table)
        except PolicyViolationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    @router.post("/{connection}/query/explain", response_model=ExplainResult)
    async def explain_query(
        connection: str, query: StructuredQuery, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            return await service.explain(query)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    @router.post("/{connection}/query", response_model=StructuredQueryResult)
    async def execute_query(
        connection: str, query: StructuredQuery, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            return await service.execute(query)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    @router.post("/{connection}/query/batch", response_model=BatchQueryResult)
    async def execute_query_batch(
        connection: str, payload: BatchQueryRequest, principal: Principal = Depends(get_principal)
    ):
        service = _service(connection, principal)
        try:
            validate_batch_size(len(payload.queries), get_policy(connection, principal=principal))
        except PolicyViolationError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        results = await service.execute_many(payload.queries)
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
                connections_file=cfg.connections_file, policy_file=cfg.policy_file
            )
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    return router
