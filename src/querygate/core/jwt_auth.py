"""JWT bearer-token authentication — a second `Authenticator` alongside
`ApiKeyAuthenticator` (see `core/auth.py`), selectable via config.

Static bearer tokens (`ApiKeyAuthenticator`) don't give per-caller identity
beyond a single shared subject per key, expiry, or integration with an
existing identity provider. `JwtAuthenticator` verifies a bearer token's
signature against a JWKS endpoint and maps its claims to a `Principal`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Mapping, Optional

import jwt

from querygate.core.auth import Actor, Principal
from querygate.core.logging import get_logger

if TYPE_CHECKING:
    from querygate.core.config import AppConfig

# Defence-in-depth bound on how deep a nested `act` (delegation) chain we will
# walk. The token is IdP-signed so this is not the trust boundary, but a
# bounded walk keeps a pathological chain from causing unbounded work.
_MAX_ACT_DEPTH = 16


class JwtAuthenticator:
    """Verifies bearer tokens as JWTs against a JWKS endpoint.

    Signing keys are fetched from `jwks_url` and cached in-process
    (`jwt.PyJWKClient`'s own cache, default 5-minute lifespan) — the JWKS
    endpoint is hit on a cache miss only, not on every request. `authenticate`
    stays synchronous to match the `Authenticator` protocol used by both
    `api/auth.py` (a FastAPI dependency) and `mcp/auth.py` (ASGI middleware),
    so a cache-miss JWKS fetch briefly blocks the event loop — an accepted
    tradeoff given how rarely it happens (once per `lifespan`, not per
    request) rather than making `Authenticator.authenticate` async, which
    would ripple into both transports.
    """

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: Optional[str] = None,
        audience: Optional[str] = None,
        algorithms: Optional[List[str]] = None,
        subject_claim: str = "sub",
        scopes_claim: str = "scope",
        act_claim: str = "act",
        leeway_seconds: float = 0,
    ) -> None:
        self._jwk_client = jwt.PyJWKClient(jwks_url, cache_keys=True)
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms or ["RS256"]
        self._subject_claim = subject_claim
        self._scopes_claim = scopes_claim
        self._act_claim = act_claim
        self._leeway_seconds = leeway_seconds

    def authenticate(self, bearer_token: Optional[str]) -> Optional[Principal]:
        if not bearer_token:
            return None
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(bearer_token)
            claims = jwt.decode(
                bearer_token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": [self._subject_claim]},
            )
        except jwt.PyJWTError as exc:
            # Never log the token itself — only why verification failed.
            get_logger().warning("jwt.auth.rejected", error=str(exc))
            return None

        subject = claims.get(self._subject_claim)
        if not subject:
            return None
        # An RFC 8693 token-exchange access token carries the ultimate subject
        # (the human) in `sub` and the delegated actor (the agent) in `act`. We
        # map `sub` -> Principal.subject so *the human's* policy applies, and
        # `act` -> Principal.actor for dual-identity attribution. Absent `act`,
        # the caller is non-delegated and behaves exactly as before.
        return Principal(
            subject=str(subject),
            scopes=frozenset(_extract_scopes(claims, self._scopes_claim)),
            claims=claims,
            auth_method="jwt",
            actor=_extract_actor(claims.get(self._act_claim)),
        )


def build_jwt_authenticator(cfg: "AppConfig") -> Optional[JwtAuthenticator]:
    """Shared by `api/auth.py` and `mcp/auth.py` — one identity provider for
    both surfaces (see `AppConfig`'s `jwt_*` fields). Returns None when JWT
    auth isn't enabled, so callers can fall back to API-key-only.
    """
    if not cfg.jwt_enabled:
        return None
    return JwtAuthenticator(
        jwks_url=cfg.jwt_jwks_url,
        issuer=cfg.jwt_issuer or None,
        audience=cfg.jwt_audience or None,
        algorithms=cfg.jwt_algorithms,
        subject_claim=cfg.jwt_subject_claim,
        scopes_claim=cfg.jwt_scopes_claim,
        act_claim=cfg.jwt_act_claim,
        leeway_seconds=cfg.jwt_leeway_seconds,
    )


def _extract_actor(act: Any, _depth: int = 0) -> Optional[Actor]:
    """Build a delegation chain from an RFC 8693 `act` (actor) claim.

    `act` is a JSON object with a `sub` and an optional nested `act` (the prior
    actor). We walk it iteratively under a depth bound (`_MAX_ACT_DEPTH`) so a
    pathological nesting can't cause unbounded work, then thread the frames into
    the immutable `Actor` chain (immediate actor outermost).
    """
    subjects: List[str] = []
    node: Any = act
    while isinstance(node, Mapping) and len(subjects) < _MAX_ACT_DEPTH:
        sub = node.get("sub")
        if not sub:
            break
        subjects.append(str(sub))
        node = node.get("act")
    actor: Optional[Actor] = None
    for sub in reversed(subjects):
        actor = Actor(subject=sub, delegated_by=actor)
    return actor


def _extract_scopes(claims: dict, scopes_claim: str) -> List[str]:
    raw: Any = claims.get(scopes_claim)
    if raw is None:
        return []
    if isinstance(raw, str):
        return raw.split()  # OAuth2 "scope" claim is a space-delimited string
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []
