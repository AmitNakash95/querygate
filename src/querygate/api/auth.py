"""REST authentication, built on the shared core.auth abstraction.

Accepts a static API key (`ApiKeyAuthenticator`) and, when configured, a JWT
(`core/jwt_auth.JwtAuthenticator`) — see `core/auth.CompositeAuthenticator`.
"""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Header, HTTPException, status

from querygate.core.auth import (
    Authenticator,
    AnonymousAuthenticator,
    ApiKeyAuthenticator,
    CompositeAuthenticator,
    Principal,
    extract_bearer_token,
)
from querygate.core.config import AppConfig
from querygate.core.jwt_auth import build_jwt_authenticator


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
    # alone isn't the gate. Once an operator sets api_keys or enables JWT,
    # even in a local/dev environment, those credentials must actually be
    # required — otherwise configuring auth in dev would silently do
    # nothing, which is exactly the bug this ordering used to have.
    if cfg.is_local and not cfg.api_keys and jwt_auth is None:
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

    def _get_current_principal(authorization: Optional[str] = Header(default=None)) -> Principal:
        token = extract_bearer_token(authorization)
        principal = authenticator.authenticate(token)
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required."
            )
        return principal

    return _get_current_principal
