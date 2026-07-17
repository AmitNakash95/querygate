"""MCP authentication and per-request caller context, built on core.auth."""

from __future__ import annotations

import contextvars

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from querygate.core.auth import ApiKeyAuthenticator, Principal, extract_bearer_token
from querygate.core.config import AppConfig
from querygate.core.logging import get_logger

_mcp_caller_var: contextvars.ContextVar[Principal] = contextvars.ContextVar("_mcp_caller")


class MCPAuthenticationError(Exception):
    """Raised when MCP auth context is missing."""


def get_mcp_caller() -> Principal:
    """Return the authenticated caller for the current MCP request."""
    try:
        return _mcp_caller_var.get()
    except LookupError as exc:
        raise MCPAuthenticationError("MCP auth context is not initialised.") from exc


def _unauth_response() -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": "UNAUTHENTICATED", "message": "Bearer token required."}},
        status_code=401,
    )


class MCPAuthMiddleware:
    """Authenticate every request to the MCP sub-app."""

    def __init__(self, app: ASGIApp, settings: AppConfig) -> None:
        self._app = app
        self._authenticator = ApiKeyAuthenticator(
            api_keys=settings.mcp_api_keys,
            subject=settings.mcp_api_key_subject,
            allow_anonymous_without_keys=settings.is_local,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        token = extract_bearer_token(request.headers.get("Authorization"))
        principal = self._authenticator.authenticate(token)

        if principal is None:
            get_logger().warning("mcp.auth.rejected", path=request.url.path)
            if scope["type"] == "http":
                response = _unauth_response()
                await response(scope, receive, send)
            return

        get_logger().debug("mcp.auth.resolved", subject=principal.subject, path=request.url.path)

        ctx_token = _mcp_caller_var.set(principal)
        try:
            await self._app(scope, receive, send)
        finally:
            _mcp_caller_var.reset(ctx_token)
