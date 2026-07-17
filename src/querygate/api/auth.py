"""REST API-key authentication, built on the shared core.auth abstraction."""

from __future__ import annotations

from typing import Callable, Optional

from fastapi import Header, HTTPException, status

from querygate.core.auth import ApiKeyAuthenticator, Principal, extract_bearer_token
from querygate.core.config import AppConfig


def build_authenticator(cfg: AppConfig) -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(
        api_keys=cfg.api_keys,
        subject=cfg.api_key_subject,
        allow_anonymous_without_keys=cfg.is_local,
    )


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
