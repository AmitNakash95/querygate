"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response

from querygate.api.auth import build_principal_dependency
from querygate.api.routes import build_router
from querygate.core.config import AppConfig
from querygate.core.config import config as default_config
from querygate.core.logging import ContextLogger, context_logger, get_logger


def create_app(cfg: Optional[AppConfig] = None) -> FastAPI:
    """Build and return the configured FastAPI application."""
    conf = cfg or default_config

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log = get_logger()
        log.info("querygate.startup", environment=conf.environment)
        mcp_task: Optional[asyncio.Task] = None

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
    application.include_router(build_router(principal_dependency, prefix=conf.api_v1_prefix))

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
    async def health() -> dict:
        return {"status": "ok", "service": "querygate", "version": conf.app_version}

    return application


app = create_app()
