"""Admin connection-operations API (TODO.md item 43, phases 1 and 2a).

The public `GET /health` endpoint intentionally returns only aggregate
healthy/unhealthy/unknown counts so it cannot disclose database topology to an
unauthenticated orchestrator probe. An administrator, though, needs to know
*which* configured connection is failing, when it last succeeded, and whether
schema discovery has warmed — without reading process logs or seeing a raw
driver exception (which can embed the host/port/database/username).

This router adds an `admin:connections:read`-scoped, credential-free
per-connection status view built from the same `HealthMonitor` snapshot
`/health` already maintains. It never returns a connection string or raw driver
error; a failure surfaces only as a stable `failure_category`. It also adds a
separately `admin:connections:test`-scoped "test now" probe (phase 2a) that
triggers an immediate, rate-limited, audited re-check against one connection,
reusing the exact same ping/classification path and non-disclosure posture.
The browser workspace that renders this view is deferred (phase 2b).
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional

import pydantic as pyd
from fastapi import APIRouter, Depends, HTTPException, Request, status

from querygate.api._errors import require_scope
from querygate.audit.logger import audit_connection_probe
from querygate.connections.registry import get_registry
from querygate.core.auth import Principal
from querygate.core.config import AppConfig
from querygate.core.scopes import ADMIN_CONNECTIONS_READ_SCOPE, ADMIN_CONNECTIONS_TEST_SCOPE
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


def _status_for(enabled: bool, health: Optional[ConnectionHealth]) -> ConnectionStatusValue:
    if not enabled:
        # Deployment-disabled connections are not health-monitored at all.
        return "disabled"
    if health is None or health.healthy is None:
        return "unknown"
    return "healthy" if health.healthy else "degraded"


def _to_connection_status(info, health: Optional[ConnectionHealth]) -> "ConnectionStatus":
    reachable = info.enabled and health is not None and health.healthy is not None
    return ConnectionStatus(
        connection_id=info.id,
        dialect=info.dialect,
        enabled=info.enabled,
        status=_status_for(info.enabled, health),
        last_checked=health.last_checked if reachable else None,
        last_success=health.last_success if health is not None else None,
        latency_ms=health.latency_ms if health is not None else None,
        schema_reflected=is_schema_cached(info.id),
        failure_category=(
            health.failure_category if health is not None and health.healthy is False else None
        ),
    )


def build_admin_connections_router(
    get_principal: Callable[..., Principal], cfg: AppConfig, prefix: str = "/api/v1"
) -> APIRouter:
    router = APIRouter(prefix=f"{prefix}/admin/connections")

    @router.get("", response_model=List[ConnectionStatus])
    async def list_connection_status(
        request: Request, principal: Principal = Depends(get_principal)
    ):
        require_scope(principal, ADMIN_CONNECTIONS_READ_SCOPE)
        monitor = getattr(request.app.state, "health_monitor", None)
        snapshot = monitor.snapshot() if monitor is not None else {}

        statuses = [
            _to_connection_status(info, snapshot.get(info.id))
            for info in get_registry().list_public()
        ]
        statuses.sort(key=lambda s: s.connection_id)
        return statuses

    @router.post("/{connection_id}/test", response_model=ConnectionStatus)
    async def test_connection_endpoint(
        connection_id: str, request: Request, principal: Principal = Depends(get_principal)
    ):
        require_scope(principal, ADMIN_CONNECTIONS_TEST_SCOPE)
        info = next((i for i in get_registry().list_public() if i.id == connection_id), None)
        if info is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown connection: {connection_id!r}",
            )
        if not info.enabled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Connection {connection_id!r} is disabled",
            )
        monitor = getattr(request.app.state, "health_monitor", None)
        if monitor is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Health monitoring is not running",
            )

        remaining = monitor.seconds_until_manual_test_allowed(
            connection_id, cfg.admin_connection_test_cooldown_seconds
        )
        if remaining > 0:
            audit_connection_probe(
                connection_id=connection_id,
                outcome="rejected",
                principal=principal.subject,
                principal_scopes=sorted(principal.scopes),
                auth_method=principal.auth_method,
                error_category="rate_limited",
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"A test was already requested recently for {connection_id!r}; "
                    f"retry in {remaining:.0f}s"
                ),
                headers={"Retry-After": str(int(remaining) + 1)},
            )

        start = time.monotonic()
        health = await monitor.manual_check(connection_id)
        duration_ms = int((time.monotonic() - start) * 1000)

        audit_connection_probe(
            connection_id=connection_id,
            outcome="success",
            principal=principal.subject,
            principal_scopes=sorted(principal.scopes),
            auth_method=principal.auth_method,
            probe_healthy=health.healthy,
            failure_category=health.failure_category,
            latency_ms=health.latency_ms,
            duration_ms=duration_ms,
        )
        return _to_connection_status(info, health)

    return router
