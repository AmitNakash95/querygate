"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette import status

from querygate.api.admin_config_routes import build_admin_config_router
from querygate.api.admin_connections_routes import build_admin_connections_router
from querygate.api.admin_observability_routes import build_admin_observability_router
from querygate.api.admin_ui_routes import build_admin_ui_router
from querygate.api._errors import install_exception_handlers, require_scope
from querygate.api.auth import build_principal_dependency
from querygate.api.catalog_governance_routes import build_catalog_governance_router
from querygate.api.help_routes import build_help_router
from querygate.api.routes import build_router
from querygate.audit.ledger import resolve_ledger_key
from querygate.admin.observed_shapes import (
    clear_redis_observed_shape_store,
    configure_observed_shape_store,
)
from querygate.audit.sinks import configure_audit_sink, reset_audit_sink
from querygate.catalog.refresh import CatalogRefreshMonitor
from querygate.catalog.usage import CatalogUsageLearningMonitor
from querygate.config_reload import CredentialLeaseMonitor
from querygate.core.auth import Principal
from querygate.core.config import AppConfig, AuditSinkBackend, ConcurrencyBackend
from querygate.core.config import config as default_config
from querygate.core.logging import ContextLogger, context_logger, get_logger
from querygate.core.scopes import ADMIN_METRICS_READ_SCOPE
from querygate.execution.concurrency import clear_redis_limiter, init_redis_limiter
from querygate.execution.disclosure_budget import clear_redis_disclosure_budget_limiter
from querygate.execution.quota import clear_redis_quota_limiter
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

        # Started before configure_audit_sink() so the WORM sink's buffer
        # singleton has a monitor actually draining it from the moment the
        # sink can first receive an event — TODO.md item 134.
        worm_flush_monitor: Optional["WormFlushMonitor"] = None
        if conf.audit_sink_backend == AuditSinkBackend.JSONL_CHAINED_S3_WORM:
            from querygate.audit.worm_sink import WormFlushMonitor

            worm_ledger_key = resolve_ledger_key(conf.audit_ledger_hmac_key)
            if worm_ledger_key is None:
                # TODO.md item 154 / docs/THREAT_MODEL.md QG-40: an unkeyed
                # (SHA-256) chain detects accidental corruption but not a
                # deliberate forgery — anyone with s3:PutObject on the
                # archive prefix can compute a valid unkeyed envelope
                # themselves. Surfaced at startup, not just in docs, since
                # this is the shipped default and easy to miss.
                log.warning(
                    "audit.worm.unkeyed_chain",
                    detail=(
                        "AUDIT_LEDGER_HMAC_KEY is unset: WORM archive segments will be "
                        "chained unkeyed (SHA-256), which detects corruption but not a "
                        "deliberate forgery by anyone with s3:PutObject on the archive "
                        "prefix. Set AUDIT_LEDGER_HMAC_KEY for forgery-resistant segments."
                    ),
                )
            worm_flush_monitor = WormFlushMonitor(
                bucket=conf.audit_worm_s3_bucket,
                prefix=conf.audit_worm_s3_prefix,
                region=conf.audit_worm_s3_region,
                retention_mode=conf.audit_worm_retention_mode,
                retention_days=conf.audit_worm_retention_days,
                interval_seconds=conf.audit_worm_flush_interval_seconds,
                ledger_key=worm_ledger_key,
            )
            await worm_flush_monitor.start()
        app.state.worm_flush_monitor = worm_flush_monitor

        # TODO.md item 195: size the observed-shape store from config before
        # any request can record into it. Off by default; when disabled the
        # store simply never receives a record() call.
        configure_observed_shape_store(
            conf.observed_shapes_max_entries, enabled=conf.observed_shapes_enabled
        )

        configure_audit_sink(
            backend=conf.audit_sink_backend.value,
            jsonl_path=conf.audit_jsonl_path,
            fsync=conf.audit_jsonl_fsync,
            ledger_hmac_key=conf.audit_ledger_hmac_key,
        )

        health_monitor = HealthMonitor(conf.health_check_interval_seconds)
        await health_monitor.start()
        app.state.health_monitor = health_monitor

        catalog_refresh_monitor: Optional[CatalogRefreshMonitor] = None
        if conf.semantic_memory_refresh_enabled:
            catalog_refresh_monitor = CatalogRefreshMonitor(
                catalog_file=conf.catalog_file or "",
                interval_seconds=conf.semantic_memory_refresh_interval_seconds,
                max_tables=conf.semantic_memory_refresh_max_tables,
            )
            await catalog_refresh_monitor.start()
        app.state.catalog_refresh_monitor = catalog_refresh_monitor

        catalog_usage_learning_monitor: Optional[CatalogUsageLearningMonitor] = None
        if conf.semantic_memory_learning_enabled:
            catalog_usage_learning_monitor = CatalogUsageLearningMonitor(
                catalog_file=conf.catalog_file or "",
                interval_seconds=conf.semantic_memory_learning_interval_seconds,
            )
            await catalog_usage_learning_monitor.start()
        app.state.catalog_usage_learning_monitor = catalog_usage_learning_monitor

        credential_lease_monitor: Optional[CredentialLeaseMonitor] = None
        if conf.credential_lease_refresh_enabled:
            credential_lease_monitor = CredentialLeaseMonitor(
                cfg=conf,
                poll_interval_seconds=conf.credential_lease_check_interval_seconds,
                refresh_margin_seconds=conf.credential_lease_refresh_margin_seconds,
            )
            await credential_lease_monitor.start()
        app.state.credential_lease_monitor = credential_lease_monitor

        redis_client = None
        if conf.concurrency_backend == ConcurrencyBackend.REDIS:
            import redis.asyncio as redis_asyncio

            from querygate.execution.redis_concurrency import RedisConcurrencyLimiter

            from querygate.execution.disclosure_budget import (
                init_redis_disclosure_budget_limiter,
            )
            from querygate.execution.quota import init_redis_quota_limiter
            from querygate.execution.redis_disclosure_budget import (
                RedisDisclosureBudgetLimiter,
            )
            from querygate.execution.redis_quota import RedisQuotaLimiter

            redis_client = redis_asyncio.Redis.from_url(conf.concurrency_redis_url)
            init_redis_limiter(
                RedisConcurrencyLimiter(
                    redis_client,
                    lease_seconds=conf.concurrency_redis_lease_seconds,
                    poll_interval_seconds=conf.concurrency_redis_poll_interval_seconds,
                    fail_open=conf.concurrency_redis_fail_open,
                )
            )
            # Same Redis backs the cross-replica per-principal quota (item 50
            # phase 2), so a principal's rate/byte budget is one shared window
            # across replicas rather than one-per-replica.
            init_redis_quota_limiter(RedisQuotaLimiter(redis_client))
            # ...and the cumulative disclosure budget (item 179), for the same
            # reason and with more at stake: a per-replica probe budget gives a
            # prober N× the probes the operator configured against a k-anonymity
            # floor they believe is defended.
            init_redis_disclosure_budget_limiter(RedisDisclosureBudgetLimiter(redis_client))
            # ...and item 195's observed-shape store, which needs it for a
            # different reason than the three above: not to stop a per-replica
            # budget multiplying, but because a discovery window split across
            # replicas is INCOMPLETE, and narrowing a connection from an
            # incomplete list breaks the shapes the other replicas saw. Shared
            # and durable, so the window also survives a rolling deploy.
            if conf.observed_shapes_enabled:
                from querygate.admin.observed_shapes import (
                    init_redis_observed_shape_store,
                )
                from querygate.admin.redis_observed_shapes import (
                    RedisObservedShapeStore,
                )

                init_redis_observed_shape_store(
                    RedisObservedShapeStore(
                        redis_client,
                        max_entries=conf.observed_shapes_max_entries,
                        ttl_seconds=conf.observed_shapes_ttl_seconds,
                        enabled=True,
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
            if catalog_refresh_monitor is not None:
                await catalog_refresh_monitor.stop()
            if catalog_usage_learning_monitor is not None:
                await catalog_usage_learning_monitor.stop()
            if credential_lease_monitor is not None:
                await credential_lease_monitor.stop()
            await health_monitor.stop()
            reset_audit_sink()
            if worm_flush_monitor is not None:
                await worm_flush_monitor.stop()
            if redis_client is not None:
                # Revert every module-global limiter this app installed before
                # the client it wraps is closed. Leaving the disclosure-budget
                # limiter installed matters more than the others: it fails
                # CLOSED, so a later in-process `execute()` (a second
                # `create_app`, an embedded MCP server, a worker) would refuse
                # every aggregate query on a k-floored connection against a
                # dead client.
                clear_redis_limiter()
                clear_redis_quota_limiter()
                clear_redis_disclosure_budget_limiter()
                # item 195 phase 2: the observed-shape store is a module-global
                # this app installed too, and the comment above says "every".
                # Omitting it left a second in-process app recording into and
                # reading from the previous deployment's Redis keys while
                # reporting a `shared-durable` scope it never configured.
                clear_redis_observed_shape_store()
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

    install_exception_handlers(application)

    if conf.mcp_enabled:
        from querygate.mcp.server import setup_mcp

        setup_mcp(application, conf)

    if conf.mcp_enabled and conf.mcp_oauth_resource_server_enabled:
        # RFC 9728 protected-resource metadata (TODO.md item 90 phase 2), served
        # unauthenticated from the main app so a challenged client can discover
        # the authorization server(s) for the MCP resource.
        from querygate.mcp.oauth_metadata import build_oauth_metadata_router

        application.include_router(build_oauth_metadata_router(conf))

    principal_dependency = build_principal_dependency(conf)
    application.include_router(
        build_help_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )
    application.include_router(build_router(principal_dependency, conf, prefix=conf.api_v1_prefix))
    application.include_router(
        build_admin_config_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )
    application.include_router(
        build_admin_connections_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )
    application.include_router(
        build_admin_observability_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )
    application.include_router(
        build_catalog_governance_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )
    application.include_router(
        build_admin_ui_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
    )

    admin_ui_dir = Path(__file__).resolve().parent.parent / "admin_ui"
    application.mount("/admin", StaticFiles(directory=admin_ui_dir, html=True), name="admin-ui")

    access_ui_dir = Path(__file__).resolve().parent.parent / "access_ui"
    application.mount("/access", StaticFiles(directory=access_ui_dir, html=True), name="access-ui")

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
        is_admin_ui = request.url.path == "/admin" or request.url.path.startswith("/admin/")
        is_access_ui = request.url.path == "/access" or request.url.path.startswith("/access/")
        if is_admin_ui or is_access_ui:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            # no-store for the whole control-plane surface, not just the HTML
            # entry point: the app.js/app.css assets change with every UI update,
            # and a browser caching them behind a no-store HTML shell would keep
            # showing stale UI (and could serve a superseded control plane).
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        connection_counts = {"healthy": 0, "unhealthy": 0, "unknown": 0}
        any_unhealthy = False
        for conn_status in application.state.health_monitor.snapshot().values():
            if conn_status.healthy is False:
                any_unhealthy = True
                connection_counts["unhealthy"] += 1
            elif conn_status.healthy is True:
                connection_counts["healthy"] += 1
            else:
                connection_counts["unknown"] += 1
        # A connection that hasn't been checked yet (right after startup, before
        # its first background ping lands) is treated as not-yet-proven-broken
        # rather than failing readiness — only a confirmed ping failure does.
        body = {
            "status": "degraded" if any_unhealthy else "ok",
            "service": "querygate",
            "version": conf.app_version,
            # This endpoint is intentionally unauthenticated for orchestrator
            # readiness probes, so it returns aggregate counts rather than
            # connection ids, database topology, or driver error details.
            "connections": connection_counts,
        }
        return JSONResponse(
            content=body,
            status_code=(
                status.HTTP_503_SERVICE_UNAVAILABLE if any_unhealthy else status.HTTP_200_OK
            ),
        )

    if conf.metrics_require_auth:

        @application.get("/metrics", tags=["health"])
        async def metrics(principal: Principal = Depends(principal_dependency)) -> Response:
            require_scope(principal, ADMIN_METRICS_READ_SCOPE)
            return Response(content=render_latest(), media_type=CONTENT_TYPE_LATEST)

    else:

        @application.get("/metrics", tags=["health"])
        async def metrics_unauthenticated() -> Response:
            return Response(content=render_latest(), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
