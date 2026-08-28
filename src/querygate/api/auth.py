"""REST authentication, built on the shared core.auth abstraction.

Four credential kinds reach the same `Principal`, in one deliberate order:

1. a **browser session cookie** (`identity/`), which additionally requires a
   matching CSRF token — a cookie without one is not a weaker credential, it is
   not a credential;
2. a **QueryGate-issued device token** (`qgd_…`, RFC 8628), carrying the
   identity of the human who approved it;
3. a **static API key** (`ApiKeyAuthenticator`) for service-to-service callers;
4. an **IdP-issued JWT** (`core/jwt_auth.JwtAuthenticator`).

The first two are resolved by awaiting `identity/authenticators.py`, because
their lookup is a store read; the last two stay on the synchronous
`core/auth.Authenticator` chain they always used, unchanged. Splitting on
"needs I/O" rather than converting every authenticator to async is what keeps
`ApiKeyAuthenticator` and `JwtAuthenticator` — and the MCP transport that
shares them — exactly as they were.

Order matters twice over: real credential schemes before `AnonymousAuthenticator`
(see its docstring), and cookie before bearer so a signed-in browser is not
asked for a second credential it does not have.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Header, HTTPException, Request, status

from querygate.core.auth import (
    AnonymousAuthenticator,
    ApiKeyAuthenticator,
    Authenticator,
    CompositeAuthenticator,
    Principal,
    extract_bearer_token,
)
from querygate.core.auth_policy import (
    CONSOLE_SURFACE,
    REST_SURFACE,
    AuthMethodNotPermitted,
    AuthMethodPolicy,
    surface_for_path,
)
from querygate.core.config import AppConfig
from querygate.core.jwt_auth import build_jwt_authenticator
from querygate.core.logging import get_logger


def build_authenticator(cfg: AppConfig) -> Authenticator:
    # Order matters: real credential schemes first, AnonymousAuthenticator
    # (matches unconditionally) last — see its docstring for why.
    authenticators: list[Authenticator] = [
        ApiKeyAuthenticator(
            api_keys=cfg.api_keys, subject=cfg.api_key_subject, scopes=cfg.api_key_scopes
        )
    ]
    jwt_auth = build_jwt_authenticator(cfg)
    if jwt_auth is not None:
        authenticators.append(jwt_auth)
    # Anonymous dev bypass only when NOTHING real is configured — is_local
    # alone isn't the gate. Once an operator sets api_keys, enables JWT, or
    # turns SSO on, even in a local/dev environment, a real credential must
    # actually be required — otherwise configuring auth in dev would silently
    # do nothing, which is exactly the bug this ordering used to have.
    # `is_hardened_image` is checked FIRST and separately from `is_local`
    # (TODO.md item 215). Without it, `docker run <registry>/querygate:latest` —
    # the exact one-command install this product promises — yields a deployment
    # where every request resolves to an anonymous principal, because the
    # Dockerfile set no ENVIRONMENT, the default is `localhost`, and
    # `_validate_production_auth` only fires on `production`. The first
    # connection added is then queryable by anyone who can reach the port.
    #
    # Kept as its own condition rather than folded into `is_local` so that an
    # operator who sets ENVIRONMENT=development on a container to get readable
    # logs does not thereby re-open the bypass.
    if (
        not cfg.is_hardened_image
        and cfg.is_local
        and not cfg.api_keys
        and jwt_auth is None
        and not cfg.sso_enabled
    ):
        authenticators.append(AnonymousAuthenticator())
    if len(authenticators) == 1:
        return authenticators[0]
    return CompositeAuthenticator(authenticators)


def build_principal_dependency(cfg: AppConfig) -> Callable[..., Principal]:
    """Build a FastAPI dependency bound to `cfg` — a factory rather than a
    single module-level dependency so create_app(cfg) can be exercised with
    different auth settings in tests, mirroring the MCP auth middleware.
    """
    authenticator = build_authenticator(cfg)
    # Which *kinds* of credential each REST surface accepts (item 200). Built
    # once here; enforced below against the principal the chain resolved, so a
    # credential scheme added later is governed automatically rather than
    # inheriting access to everything.
    policies = {
        CONSOLE_SURFACE: AuthMethodPolicy.build(
            cfg.console_auth_methods, surface=CONSOLE_SURFACE, sso_enabled=cfg.sso_enabled
        ),
        REST_SURFACE: AuthMethodPolicy.build(
            cfg.rest_auth_methods, surface=REST_SURFACE, sso_enabled=cfg.sso_enabled
        ),
    }
    session_authenticator = None
    device_authenticator = None
    if cfg.sso_enabled:
        from querygate.identity.authenticators import (
            DeviceTokenAuthenticator,
            SessionCookieAuthenticator,
        )

        # No idle timeout passed: the authenticator reads it from identity.yaml
        # per request, so a hot reload that shortens it takes effect at once.
        session_authenticator = SessionCookieAuthenticator()
        if cfg.sso_device_grant_enabled:
            device_authenticator = DeviceTokenAuthenticator()

    async def _get_current_principal(
        request: Request, authorization: Optional[str] = Header(default=None)
    ) -> Principal:
        if session_authenticator is not None:
            from querygate.identity.authenticators import CsrfMismatchError

            cookie = request.cookies.get(cfg.sso_session_cookie_name)
            if cookie:
                try:
                    principal = await session_authenticator.authenticate(
                        cookie, csrf_token=request.headers.get(cfg.sso_csrf_header)
                    )
                except CsrfMismatchError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
                    ) from exc
                if principal is not None:
                    return _enforce_method(principal, request)

        token = extract_bearer_token(authorization)
        if device_authenticator is not None and token:
            principal = await device_authenticator.authenticate(token)
            if principal is not None:
                return _enforce_method(principal, request)

        principal = authenticator.authenticate(token)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required."
            )
        return _enforce_method(principal, request)

    def _enforce_method(principal: Principal, request: Request) -> Principal:
        policy = policies[surface_for_path(request.url.path, cfg.api_v1_prefix)]
        try:
            policy.check(principal)
        except AuthMethodNotPermitted as exc:
            # 403, not 401: the caller authenticated fine, and retrying with the
            # same kind of credential will never succeed. A 401 would invite
            # exactly that retry.
            get_logger().warning(
                "auth.method_not_permitted",
                surface=exc.surface,
                auth_method=exc.method,
                subject=principal.subject,
                path=request.url.path,
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc
        return principal

    return _get_current_principal
