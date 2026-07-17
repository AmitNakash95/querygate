"""Pluggable authentication shared by the REST and MCP surfaces.

`Authenticator` is a narrow protocol so API-key auth (today) can be swapped
for OAuth/JWT/RBAC later without touching the REST or MCP transport code —
both `api/auth.py` and `mcp/auth.py` depend only on this interface.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass(frozen=True)
class Principal:
    """Minimal identity for an authenticated caller."""

    subject: str


class Authenticator(Protocol):
    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        """Return the caller's Principal, or None if the token is missing/invalid."""
        ...


class ApiKeyAuthenticator:
    """Constant-time bearer-token match against a configured API key list.

    When no keys are configured, `allow_anonymous_without_keys` lets non-
    production deployments run unauthenticated (local dev convenience) —
    production should always configure at least one key.
    """

    def __init__(
        self,
        api_keys: list[str],
        subject: str,
        allow_anonymous_without_keys: bool = False,
    ) -> None:
        self._api_keys = [key for key in api_keys if key]
        self._subject = subject
        self._allow_anonymous = allow_anonymous_without_keys

    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        if not self._api_keys:
            return Principal(subject="anonymous-dev") if self._allow_anonymous else None
        if bearer_token is None:
            return None
        for key in self._api_keys:
            if hmac.compare_digest(bearer_token.encode("utf-8"), key.encode("utf-8")):
                return Principal(subject=self._subject)
        return None


def extract_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[len("Bearer ") :].strip()
    return token or None
