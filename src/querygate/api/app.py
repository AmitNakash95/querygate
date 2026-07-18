"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette import status

from querygate.api.auth import build_principal_dependency
from querygate.api.routes import build_router
from querygate.core.config import AppConfig, ConcurrencyBackend
from querygate.core.config import config as default_config
from querygate.core.logging import ContextLogger, context_logger, get_logger
from querygate.execution.concurrency import clear_redis_limiter, init_redis_limiter
from querygate.health import HealthMonitor
from querygate.metrics import CONTENT_TYPE_LATEST, render_latest


def create_app(cfg: Optional[AppConfig] = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    conf = cfg or default_config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = get_logger()
        log.info("querygate.startup", environment=conf.environment)
        mcp_task: Optional[asyncio.Task] = None

        health_monitor = HealthMonitor(conf.health_check_interval_seconds)
        await health_monitor.start()
        app.state.health_monitor = health_monitor

        redis_client = None
        if conf.concurrency_backend == ConcurrencyBackend.REDIS:
            import redis.asyncio as redis_asyncio

            from querygate.execution.redis_concurrency import RedisConcurrencyLimiter

            redis_client = redis_asyncio.Redis.from_url(conf.concurrency_redis_url)
            init_redis_limiter(
                RedisConcurrencyLimiter(
                    redis_client,
                    lease_seconds=conf.concurrency_redis_lease_seconds,
                    poll_interval_seconds=conf.concurrency_redis_poll_interval_seconds,
                    fail_open=conf.concurrency_redis_fail_open,
                )
            )

        try:
            if conf.mcp_enabled:
                from querygate.mcp.server import mcp_server as mcp_srv

                mcp_ready = asyncio.Event()
                mcp_stop = asyncio.Event()

                async def _keep_mcp_alive() -> None:
                    async with mcp_srv.session_manager.run():
                        mcp_ready.set()
                        await mcp_stop.wait()

                mcp_task = asyncio.create_task(_keep_mcp_alive())
                app.state._mcp_stop = mcp_stop
                await mcp_ready.wait()

            yield
        finally:
            if mcp_task is not None:
                app.state._mcp_stop.set()
                await mcp_task
            await health_monitor.stop()
            if redis_client is not None:
                clear_redis_limiter()
                await redis_client.aclose()

    application = FastAPI(
        title="QueryGate",
        summary="Agent-safe database access gateway",
        version=conf.app_version,
        debug=conf.is_local,
        lifespan=lifespan,
        openapi_url=f"{conf.api_v1_prefix}/openapi.json" if conf.is_local else None,
        docs_url=f"{conf.api_v1_prefix}/docs" if conf.is_local else None,
        redoc_url=f"{conf.api_v1_prefix}/redoc" if conf.is_local else None,
    )

    if conf.mcp_enabled:
        from querygate.mcp.server import setup_mcp

        setup_mcp(application, conf)

    principal_dependency = build_principal_dependency(conf)
    application.include_router(build_router(principal_dependency, conf, prefix=conf.api_v1_prefix))

    @application.middleware("http")
    async def inject_request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        req_logger = ContextLogger(
            request_id=request_id, method=request.method, path=request.url.path
        )
        token = context_logger.set(req_logger)
        try:
            req_logger.info("request.incoming")
            response: Response = await call_next(request)
            req_logger.info("request.outgoing", status_code=response.status_code)
        finally:
            context_logger.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        connections = {}
        any_unhealthy = False
        for conn_id, conn_status in application.state.health_monitor.snapshot().items():
            if conn_status.healthy is False:
                any_unhealthy = True
            connections[conn_id] = {
                "healthy": conn_status.healthy,
                "last_checked": (
                    datetime.fromtimestamp(conn_status.last_checked, tz=timezone.utc).isoformat()
                    if conn_status.last_checked is not None
                    else None
                ),
                "error": conn_status.error,
            }
        # A connection that hasn't been checked yet (right after startup, before
        # its first background ping lands) is treated as not-yet-proven-broken
        # rather than failing readiness — only a confirmed ping failure does.
        body = {
            "status": "degraded" if any_unhealthy else "ok",
            "service": "querygate",
            "version": conf.app_version,
            "connections": connections,
        }
        return JSONResponse(
            content=body,
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE if any_unhealthy else status.HTTP_200_OK
            ),
        )

    @application.get("/metrics", tags=["health"])
    async def metrics() -> Response:
        return Response(content=render_latest(), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
