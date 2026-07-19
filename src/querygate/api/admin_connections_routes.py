"""Admin connection-operations API (TODO.md item 43, phase 1).

The public `GET /health` endpoint intentionally returns only aggregate
healthy/unhealthy/unknown counts so it cannot disclose database topology to an
unauthenticated orchestrator probe. An administrator, though, needs to know
*which* configured connection is failing, when it last succeeded, and whether
schema discovery has warmed — without reading process logs or seeing a raw
driver exception (which can embed the host/port/database/username).

This router adds an `admin:connections:read`-scoped, credential-free
per-connection status view built from the same `HealthMonitor` snapshot
`/health` already maintains. It never returns a connection string or raw driver
error; a failure surfaces only as a stable `failure_category`. Phase 2 (the
rate-limited "test now" probe and the browser workspace) is deferred.
"""

from __future__ import annotations

from typing import Callable, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, Request, status

from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.scopes import ADMIN_CONNECTIONS_READ_SCOPE
from querygate.health import ConnectionHealth
from querygate.schema.reflection import is_schema_cached

ConnectionStatusValue = str  # one of: healthy | degraded | disabled | unknown


class ConnectionStatus(pyd.BaseModel):
    """Credential-free operational status of one configured connection.

    Deliberately carries no connection string and no raw driver error — a
    failure is reported only as the stable `failure_category`.
    """

    connection_id: str
    dialect: str
    enabled: bool
    status: ConnectionStatusValue
    last_checked: Optional[float] = None
    last_success: Optional[float] = None
    latency_ms: Optional[float] = None
    schema_reflected: bool = False
    failure_category: Optional[str] = None

    model_config = pyd.ConfigDict(extra="forbid")


def _require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing required scope: {scope!r}"
        )


def _status_for(enabled: bool, health: Optional[ConnectionHealth]) -> ConnectionStatusValue:
    if not enabled:
        # Deployment-disabled connections are not health-monitored at all.
        return "disabled"
    if health is None or health.healthy is None:
        return "unknown"
    return "healthy" if health.healthy else "degraded"


def build_admin_connections_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/connections")

    @router.get("", response_model=List[ConnectionStatus])
    async def list_connection_status(
        request: Request, principal: Principal = Depends(get_principal)
    ):
        _require_scope(principal, ADMIN_CONNECTIONS_READ_SCOPE)
        monitor = getattr(request.app.state, "health_monitor", None)
        snapshot = monitor.snapshot() if monitor is not None else {}

        statuses: List[ConnectionStatus] = []
        for info in get_registry().list_public():
            health = snapshot.get(info.id)
            reachable = info.enabled and health is not None and health.healthy is not None
            statuses.append(
                ConnectionStatus(
                    connection_id=info.id,
                    dialect=info.dialect,
                    enabled=info.enabled,
                    status=_status_for(info.enabled, health),
                    last_checked=health.last_checked if reachable else None,
                    last_success=health.last_success if health is not None else None,
                    latency_ms=health.latency_ms if health is not None else None,
                    schema_reflected=is_schema_cached(info.id),
                    failure_category=(
                        health.failure_category
                        if health is not None and health.healthy is False
                        else None
                    ),
                )
            )
        statuses.sort(key=lambda s: s.connection_id)
        return statuses

    return router
