"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import uuid

import httpx
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
from querygate.subscription.observability import current_signal, publish_signal


def create_app(cfg: Optional[AppConfig] = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    conf = cfg or default_config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = get_logger()
        log.info("querygate.startup", environment=conf.environment)

        # First boot, before ANY store is constructed (item 215). Seeds
        # connections/policy/catalog only when absent and generates an admin key
        # exactly once. Every write is create-if-absent, so a restart, a rebuild,
        # a replica and a crash-loop all reach the same state without
        # regenerating — silently rotating a key on a rebuilt image would turn a
        # routine upgrade into an unplanned re-activation.
        if conf.is_hardened_image:
            from querygate.bootstrap import first_boot

            result = first_boot(Path(conf.var_dir))
            if result.is_first_boot:
                log.info("querygate.first_boot", seeded=list(result.seeded))
                if result.generated_admin_key:
                    # Printed once, to the operator's terminal. There is no
                    # writable secret backend on a fresh install — `env:` is the
                    # only registered resolver and is not writable from a
                    # request — so this is the honest delivery mechanism, and
                    # the log line says so rather than implying a vault.
                    log.warning(
                        "querygate.first_boot.admin_key_generated",
                        path=str(Path(conf.var_dir) / "admin-api-key"),
                    )

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
                endpoint_url=conf.audit_worm_s3_endpoint_url,
            )
            if conf.audit_worm_s3_endpoint_url:
                # TODO.md item 201: an S3-compatible endpoint is only a WORM
                # archive if that store actually implements Object Lock. We
                # cannot verify it here without a write, and the failure is
                # silent and total — objects that look archived but are
                # ordinary deletable blobs — so say so once at startup.
                log.warning(
                    "audit.worm.custom_endpoint",
                    detail=(
                        "AUDIT_WORM_S3_ENDPOINT_URL is set — WORM retention is only "
                        "real if this store implements S3 Object Lock. AWS S3 itself "
                        "REJECTS a PutObject carrying Object Lock headers against a "
                        "bucket without Object Lock (surfacing as a counted flush "
                        "failure), but an S3-compatible store that ignores those "
                        "headers would accept the write and produce ordinary, "
                        "deletable objects. Verify COMPLIANCE-mode retention against "
                        "this store before relying on the archive for compliance."
                    ),
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

        # The subscription refresher (item 211). Started last of the monitors so
        # a start-up failure here is unambiguous, and holding its own httpx
        # client so the one outbound call this product makes has a single owner.
        subscription_manager = None
        subscription_http_client = None
        if conf.subscription_enabled:
            from querygate.subscription.bootstrap import build_manager

            if conf.subscription_mode == "http":
                subscription_http_client = httpx.AsyncClient()
            subscription_manager = build_manager(conf, client=subscription_http_client)
            await subscription_manager.start()
        app.state.subscription_manager = subscription_manager
        app.state.subscription_http_client = subscription_http_client

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
            # ...and item 199's SSO state. Different failure mode again: a
            # per-replica session store does not multiply a budget or split a
            # window, it signs people OUT at random, because the replica that
            # answers their next request never saw them log in. Sessions, in-
            # flight logins, device grants and issued tokens all move together —
            # a device token that only works on one replica is the same defect.
            if conf.sso_enabled:
                from querygate.identity.device import (
                    set_device_grant_store,
                    set_issued_token_store,
                )
                from querygate.identity.redis_sessions import (
                    RedisDeviceGrantStore,
                    RedisIssuedTokenStore,
                    RedisLoginFlowStore,
                    RedisSessionStore,
                )
                from querygate.identity.sessions import (
                    set_login_flow_store,
                    set_session_store,
                )

                set_session_store(RedisSessionStore(redis_client))
                set_login_flow_store(RedisLoginFlowStore(redis_client))
                set_device_grant_store(RedisDeviceGrantStore(redis_client))
                set_issued_token_store(RedisIssuedTokenStore(redis_client))

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
            if subscription_manager is not None:
                await subscription_manager.stop()
            if subscription_http_client is not None:
                await subscription_http_client.aclose()
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
                if conf.sso_enabled:
                    # Drop back to the in-process stores before the client
                    # closes, so nothing can reach a dead connection during
                    # shutdown and report "not signed in" as though the session
                    # had expired.
                    from querygate.identity.device import (
                        set_device_grant_store,
                        set_issued_token_store,
                    )
                    from querygate.identity.sessions import (
                        set_login_flow_store,
                        set_session_store,
                    )

                    set_session_store(None)
                    set_login_flow_store(None)
                    set_device_grant_store(None)
                    set_issued_token_store(None)
                await redis_client.aclose()

    application = FastAPI(
        title="QueryGate",
        summary="Agent-safe database access gateway",
        version=conf.app_version,
        debug=conf.is_local and not conf.is_hardened_image,
        lifespan=lifespan,
        # Off in the shipped image regardless of environment (item 215): a
        # public schema plus FastAPI's debug traceback page is a reconnaissance
        # surface an operator did not ask for by choosing a log level.
        openapi_url=(
            f"{conf.api_v1_prefix}/openapi.json"
            if conf.is_local and not conf.is_hardened_image
            else None
        ),
        docs_url=(
            f"{conf.api_v1_prefix}/docs" if conf.is_local and not conf.is_hardened_image else None
        ),
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
    if conf.sso_enabled:
        # Human sign-in (TODO.md item 199). Mounted only when SSO is enabled, so
        # a deployment that has not turned it on exposes no login surface at all
        # — not even an endpoint that 404s with a distinguishable body.
        from querygate.api.admin_identity_routes import build_admin_identity_router
        from querygate.api.sso_routes import build_sso_router
        from querygate.identity.config_store import ensure_identity_store

        # Load identity.yaml here, at construction, so a missing or malformed
        # file (a bad scope name in a mapping rule, an issuer that isn't https,
        # two OIDC providers sharing a subject namespace) refuses to start
        # instead of starting cleanly and 500-ing on the first person who tries
        # to sign in. Same posture as the connections/policy stores, whose
        # validation an operator likewise wants at boot.
        ensure_identity_store(conf)

        application.include_router(
            build_sso_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
        )
        application.include_router(
            build_admin_identity_router(principal_dependency, conf, prefix=conf.api_v1_prefix)
        )

        if conf.dev_idp_enabled:
            # QueryGate's own throwaway OpenID Connect provider, so the real
            # redirect flow runs offline (TODO.md item 199 phase 1b). Mounted
            # only when explicitly enabled AND the environment is local; the
            # router builder refuses on its own if either is untrue, and the
            # config validator refuses to start at all.
            from querygate.identity.config_store import get_identity_store
            from querygate.identity.dev_idp import (
                DevIdentityProvider,
                DevPersona,
                build_dev_idp_router,
                derive_personas,
            )

            identity_store = get_identity_store()
            dev_profile = identity_store.dev_provider()
            if dev_profile is not None:
                personas = [
                    DevPersona(
                        sub=user.sub,
                        name=user.name or user.sub,
                        email=user.email or f"{user.sub}@localhost",
                        claims={**({"groups": user.groups} if user.groups else {}), **user.claims},
                        describes=user.describes,
                    )
                    for user in dev_profile.users
                ] or derive_personas(identity_store, dev_profile.id)
                application.state.dev_idp = DevIdentityProvider(
                    issuer=dev_profile.issuer, personas=personas
                )
                application.include_router(
                    build_dev_idp_router(conf, dev_profile.id, dev_profile.issuer)
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
            # A coarse enum and nothing more (TODO.md item 216): ok |
            # renewal_due | expired. This endpoint is unauthenticated by design
            # for orchestrator probes, so an expiry date, a day count, a plan or
            # an org id here would tell any scanner exactly when this customer's
            # gateway stops serving. The countdown lives on the authenticated
            # banner, the renewal email and `querygate-license status`.
            "subscription": current_signal().value,
        }
        # An expired subscription is deliberately **not** a 503. The process is
        # healthy and is refusing on a billing decision; returning "unavailable"
        # would make an orchestrator kill and restart the pod in a loop, turning
        # a renewal conversation into an outage that looks like a crash.
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
            publish_signal()
            return Response(content=render_latest(), media_type=CONTENT_TYPE_LATEST)

    else:

        @application.get("/metrics", tags=["health"])
        async def metrics_unauthenticated() -> Response:
            publish_signal()
            return Response(content=render_latest(), media_type=CONTENT_TYPE_LATEST)

    return application


app = create_app()
