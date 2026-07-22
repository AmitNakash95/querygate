"""MCP authentication and per-request caller context, built on core.auth."""

from __future__ import annotations

import contextvars

from starlette import status
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from querygate.core.auth import (
    AnonymousAuthenticator,
    ApiKeyAuthenticator,
    Authenticator,
    CompositeAuthenticator,
    Principal,
    extract_bearer_token,
)
from querygate.core.config import AppConfig
from querygate.core.jwt_auth import build_jwt_authenticator
from querygate.core.logging import get_logger

_mcp_caller_var: contextvars.ContextVar[Principal] = contextvars.ContextVar("_mcp_caller")
_mcp_config_var: contextvars.ContextVar[AppConfig] = contextvars.ContextVar("_mcp_config")


class MCPAuthenticationError(Exception):
    """Raised when MCP auth context is missing."""


def get_mcp_caller() -> Principal:
    """Return the authenticated caller for the current MCP request."""
    try:
        return _mcp_caller_var.get()
    except LookupError as exc:
        raise MCPAuthenticationError("MCP auth context is not initialised.") from exc


def get_mcp_config() -> AppConfig:
    """Return the application configuration bound to this MCP request."""
    try:
        return _mcp_config_var.get()
    except LookupError as exc:
        raise MCPAuthenticationError("MCP config context is not initialised.") from exc


def _unauth_response() -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": "UNAUTHENTICATED", "message": "Bearer token required."}},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


class MCPAuthMiddleware:
    """Authenticate every request to the MCP sub-app."""

    def __init__(self, app: ASGIApp, settings: AppConfig) -> None:
        self._app = app
        self._settings = settings
        # Order matters: real credential schemes first, AnonymousAuthenticator
        # (matches unconditionally) last — see its docstring for why.
        authenticators: list[Authenticator] = [
            ApiKeyAuthenticator(
                api_keys=settings.mcp_api_keys,
                subject=settings.mcp_api_key_subject,
                scopes=settings.mcp_api_key_scopes,
            )
        ]
        jwt_auth = build_jwt_authenticator(settings)
        if jwt_auth is not None:
            authenticators.append(jwt_auth)
        # Anonymous dev bypass only when NOTHING real is configured — see
        # api/auth.py's build_authenticator for why is_local alone isn't
        # the gate.
        if settings.is_local and not settings.mcp_api_keys and jwt_auth is None:
            authenticators.append(AnonymousAuthenticator())
        self._authenticator: Authenticator = (
            authenticators[0]
            if len(authenticators) == 1
            else CompositeAuthenticator(authenticators)
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

        get_logger().debug(
            "mcp.auth.resolved",
            subject=principal.subject,
            actor=principal.actor_subject,
            scopes=sorted(principal.scopes),
            path=request.url.path,
        )

        ctx_token = _mcp_caller_var.set(principal)
        config_token = _mcp_config_var.set(self._settings)
        try:
            await self._app(scope, receive, send)
        finally:
            _mcp_config_var.reset(config_token)
            _mcp_caller_var.reset(ctx_token)
