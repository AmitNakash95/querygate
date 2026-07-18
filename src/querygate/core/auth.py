"""Pluggable authentication shared by the REST and MCP surfaces.

`Authenticator` is a narrow protocol so API-key auth (today) can be swapped
for OAuth/JWT/RBAC later without touching the REST or MCP transport code —
both `api/auth.py` and `mcp/auth.py` depend only on this interface.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol


@dataclass(frozen=True)
class Principal:
    """Identity for an authenticated caller.

    `scopes`/`claims` are extensible by design, ahead of any consumer that
    actually needs them — a JWT-based `Authenticator` (per-caller roles) or
    per-principal policy (e.g. `mandatory_row_filters.value` resolved from a
    `tenant_id` claim) can populate them without another breaking change to
    this type.
    """

    subject: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    claims: Mapping[str, Any] = field(default_factory=dict)


class Authenticator(Protocol):
    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        """Return the caller's Principal, or None if the token is missing/invalid."""
        ...


class ApiKeyAuthenticator:
    """Constant-time bearer-token match against a configured API key list.

    Returns None (not a match) when no keys are configured, rather than
    silently accepting anything — that would make a `CompositeAuthenticator`
    ordering bug easy to write: if this authenticator matched *anything*
    whenever `api_keys` is empty, it would short-circuit before a real
    `JwtAuthenticator` further down the chain ever got to see the token, no
    matter what that token actually was. Local-dev anonymous access is
    `AnonymousAuthenticator`'s job instead, placed last in the chain.
    """

    def __init__(
        self,
        api_keys: list[str],
        subject: str,
        scopes: Optional[list[str]] = None,
    ) -> None:
        self._api_keys = [key for key in api_keys if key]
        self._subject = subject
        self._scopes = frozenset(scopes or [])

    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        if not self._api_keys or bearer_token is None:
            return None
        for key in self._api_keys:
            if hmac.compare_digest(bearer_token.encode("utf-8"), key.encode("utf-8")):
                return Principal(subject=self._subject, scopes=self._scopes)
        return None


class AnonymousAuthenticator:
    """Unconditionally succeeds — local/dev convenience so QueryGate runs
    unauthenticated when no keys are configured. Must be placed *last* in a
    `CompositeAuthenticator` chain: since it matches any input, anything
    ahead of it (a real API key, a real JWT) would never get a chance to
    reject an invalid credential — it'd just fall through to "anonymous"
    instead, silently downgrading a rejected credential into an accepted
    request. Production deployments shouldn't include this authenticator at
    all (see `AppConfig.is_local` gating in `api/auth.py`/`mcp/auth.py`).
    """

    def __init__(self, subject: str = "anonymous-dev") -> None:
        self._subject = subject

    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        return Principal(subject=self._subject)


class CompositeAuthenticator:
    """Tries each authenticator in order, returning the first match.

    Lets a deployment accept both a static API key (service-to-service) and
    a JWT (see `core/jwt_auth.JwtAuthenticator`, human/SSO callers) on the
    same endpoint without either transport (`api/auth.py`, `mcp/auth.py`)
    needing to know which scheme actually authenticated a given request —
    both still depend only on the `Authenticator` protocol.
    """

    def __init__(self, authenticators: list[Authenticator]) -> None:
        self._authenticators = authenticators

    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        for authenticator in self._authenticators:
            principal = authenticator.authenticate(bearer_token)
            if principal is not None:
                return principal
        return None


def extract_bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header[len("Bearer ") :].strip()
    return token or None
