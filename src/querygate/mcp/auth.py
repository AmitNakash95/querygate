"""MCP authentication and per-request caller context, built on core.auth.

When `mcp_oauth_resource_server_enabled` is set (TODO.md item 90 phase 2), this
middleware also makes the MCP surface a conformant OAuth 2.0 resource server per
the MCP 2026-07-28 authorization spec: it enforces RFC 8707 audience binding on
verified tokens (a token must be issued *for this resource*, blocking
confused-deputy reuse of a token minted for some other audience) and answers a
missing/invalid credential or an insufficient scope with an RFC 6750
`WWW-Authenticate` challenge that points back at the RFC 9728 protected-resource
metadata (see `mcp/oauth_metadata.py`), so the client can obtain / step up to a
correctly-audienced token.
"""

from __future__ import annotations

import contextvars
from typing import Any, Iterable, Optional

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
from querygate.mcp.oauth_metadata import protected_resource_metadata_path

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


def _token_audiences(principal: Principal) -> frozenset[str]:
    """The set of audiences carried by a token's `aud` claim (RFC 8707).

    `aud` may be a single string or an array of strings; anything else (absent,
    non-string) contributes nothing, so an audience check against it fails
    closed.
    """
    raw: Any = principal.claims.get("aud")
    if isinstance(raw, str):
        return frozenset({raw})
    if isinstance(raw, (list, tuple)):
        return frozenset(str(a) for a in raw if isinstance(a, str))
    return frozenset()


def _www_authenticate(resource_metadata_url: str, **params: str) -> str:
    """Build an RFC 6750 `Bearer` challenge with a `resource_metadata` hint.

    `resource_metadata` (RFC 9728 §5.1) always points the client at this
    resource's protected-resource metadata; additional `error`/`scope`/… params
    are appended in the order given.
    """
    parts = [f'{k}="{v}"' for k, v in params.items()]
    parts.append(f'resource_metadata="{resource_metadata_url}"')
    return "Bearer " + ", ".join(parts)


def _challenge_response(status_code: int, code: str, message: str, challenge: str) -> JSONResponse:
    return JSONResponse(
        content={"error": {"code": code, "message": message}},
        status_code=status_code,
        headers={"WWW-Authenticate": challenge},
    )


def _unauth_response(challenge: Optional[str] = None) -> JSONResponse:
    headers = {"WWW-Authenticate": challenge} if challenge else None
    return JSONResponse(
        content={"error": {"code": "UNAUTHENTICATED", "message": "Bearer token required."}},
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers=headers,
    )


class MCPAuthMiddleware:
    """Authenticate every request to the MCP sub-app."""

    def __init__(self, app: ASGIApp, settings: AppConfig) -> None:
        self._app = app
        self._settings = settings
        self._rs_enabled = settings.mcp_oauth_resource_server_enabled
        self._resource_identifier = settings.mcp_resource_identifier
        self._required_scopes = frozenset(settings.mcp_required_scopes)
        self._metadata_path = protected_resource_metadata_path(settings)
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
        # the gate. (RS mode requires jwt_enabled, so it never reaches here.)
        if settings.is_local and not settings.mcp_api_keys and jwt_auth is None:
            authenticators.append(AnonymousAuthenticator())
        self._authenticator: Authenticator = (
            authenticators[0]
            if len(authenticators) == 1
            else CompositeAuthenticator(authenticators)
        )

    def _resource_metadata_url(self, request: Request) -> str:
        """Absolute URL of the RFC 9728 metadata document for the challenge.

        Built from the request's own base URL so it is correct behind a
        reverse proxy that sets forwarded host/scheme headers.
        """
        return str(request.base_url).rstrip("/") + self._metadata_path

    def _authorize_resource_server(
        self, principal: Principal, request: Request
    ) -> Optional[JSONResponse]:
        """RS-mode checks after authentication: audience binding, then scope.

        Returns a challenge response to short-circuit with, or None to proceed.
        """
        metadata_url = self._resource_metadata_url(request)
        # RFC 8707 audience binding applies to verified bearer tokens (JWTs).
        # Static API keys are an explicitly-configured, out-of-band trust and
        # carry no `aud`, so they are not subject to audience binding — but
        # scope enforcement below still applies to them.
        if principal.auth_method == "jwt" and self._resource_identifier not in _token_audiences(
            principal
        ):
            get_logger().warning(
                "mcp.auth.audience_mismatch",
                subject=principal.subject,
                path=request.url.path,
            )
            return _challenge_response(
                status.HTTP_401_UNAUTHORIZED,
                "INVALID_TOKEN",
                "Access token is not audience-bound to this resource.",
                _www_authenticate(
                    metadata_url,
                    error="invalid_token",
                    error_description="Token audience does not include this resource.",
                ),
            )
        missing = self._missing_scopes(principal.scopes)
        if missing:
            get_logger().warning(
                "mcp.auth.insufficient_scope",
                subject=principal.subject,
                path=request.url.path,
            )
            return _challenge_response(
                status.HTTP_403_FORBIDDEN,
                "INSUFFICIENT_SCOPE",
                "Caller is missing a scope required for the MCP resource.",
                _www_authenticate(
                    metadata_url,
                    error="insufficient_scope",
                    scope=" ".join(sorted(self._required_scopes)),
                ),
            )
        return None

    def _missing_scopes(self, held: Iterable[str]) -> frozenset[str]:
        return self._required_scopes - frozenset(held)

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
                challenge = (
                    _www_authenticate(self._resource_metadata_url(request))
                    if self._rs_enabled
                    else None
                )
                response = _unauth_response(challenge)
                await response(scope, receive, send)
            return

        if self._rs_enabled:
            denial = self._authorize_resource_server(principal, request)
            if denial is not None:
                if scope["type"] == "http":
                    await denial(scope, receive, send)
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
