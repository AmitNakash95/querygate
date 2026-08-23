"""OIDC discovery — fetch and cache an IdP's `openid-configuration`.

Every provider publishes its authorization, token, JWKS, and end-session
endpoints at a well-known path under its issuer. Reading them instead of making
the operator type four URLs is what lets identity.yaml stay three fields, and
it means an IdP rotating an endpoint does not require a QueryGate config change.

The issuer is the trust anchor, so this module treats the fetched document as
untrusted input from a *named* party rather than as configuration:

* the document's own `issuer` must equal the configured issuer **exactly** —
  the check RFC 8414 §3.3 mandates, and the one that stops a compromised or
  misconfigured well-known path from repointing the flow at another IdP;
* every endpoint must be https (localhost excepted, for a dev Keycloak);
* the response is size- and time-bounded, and cached with a TTL so a login
  storm cannot turn into an outbound request storm.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from querygate.core.logging import get_logger

WELL_KNOWN_SUFFIX = "/.well-known/openid-configuration"
# A discovery document is a few KB; anything larger is not one.
MAX_DOCUMENT_BYTES = 256 * 1024
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CACHE_TTL_SECONDS = 900.0
# Distinct issuers we keep cached. Bounded because the cache key comes from
# configuration, not from a caller — but a bound costs nothing and removes the
# question entirely.
MAX_CACHE_ENTRIES = 64


class DiscoveryError(RuntimeError):
    """The IdP's discovery document is unreachable or untrustworthy."""


@dataclass(frozen=True)
class ProviderMetadata:
    """The endpoints QueryGate actually uses from a discovery document."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: Optional[str]
    id_token_signing_alg_values_supported: Tuple[str, ...]
    code_challenge_methods_supported: Tuple[str, ...]
    token_endpoint_auth_methods_supported: Tuple[str, ...]


def discovery_url(issuer: str) -> str:
    """The well-known URL for `issuer`, tolerating a trailing slash.

    Auth0 issuers end in `/` and Keycloak's do not; concatenating naively would
    produce a double slash that some IdPs 404. RFC 8414 says to insert the
    well-known segment after the host, but every mainstream provider serves the
    OIDC-Discovery form (suffix after the full issuer path), which is what this
    builds.
    """
    return issuer.rstrip("/") + WELL_KNOWN_SUFFIX


def _require_https(url: str, field: str, issuer: str) -> str:
    if not isinstance(url, str) or not url:
        raise DiscoveryError(f"Discovery document for {issuer} is missing {field}.")
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return url
    if parts.scheme != "https" or not parts.hostname:
        raise DiscoveryError(f"Discovery document for {issuer} has a non-https {field}: {url!r}")
    return url


def parse_metadata(issuer: str, document: dict) -> ProviderMetadata:
    """Validate a discovery document against the issuer it claims to describe."""
    declared = document.get("issuer")
    if declared != issuer:
        # Exact string equality, per RFC 8414 §3.3 — not a normalized compare.
        raise DiscoveryError(
            f"Discovery document issuer mismatch: configured {issuer!r}, document {declared!r}."
        )
    end_session = document.get("end_session_endpoint")
    return ProviderMetadata(
        issuer=issuer,
        authorization_endpoint=_require_https(
            document.get("authorization_endpoint"), "authorization_endpoint", issuer
        ),
        token_endpoint=_require_https(document.get("token_endpoint"), "token_endpoint", issuer),
        jwks_uri=_require_https(document.get("jwks_uri"), "jwks_uri", issuer),
        end_session_endpoint=(
            _require_https(end_session, "end_session_endpoint", issuer) if end_session else None
        ),
        id_token_signing_alg_values_supported=tuple(
            str(alg) for alg in document.get("id_token_signing_alg_values_supported") or ()
        ),
        code_challenge_methods_supported=tuple(
            str(method) for method in document.get("code_challenge_methods_supported") or ()
        ),
        token_endpoint_auth_methods_supported=tuple(
            str(method) for method in document.get("token_endpoint_auth_methods_supported") or ()
        ),
    )


class DiscoveryCache:
    """TTL cache of validated provider metadata, keyed by issuer."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._client = client
        self._entries: Dict[str, Tuple[float, ProviderMetadata]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, issuer: str) -> asyncio.Lock:
        lock = self._locks.get(issuer)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[issuer] = lock
        return lock

    def cached(self, issuer: str) -> Optional[ProviderMetadata]:
        entry = self._entries.get(issuer)
        if entry is None:
            return None
        fetched_at, metadata = entry
        if time.monotonic() - fetched_at > self._ttl:
            return None
        return metadata

    async def get(self, issuer: str) -> ProviderMetadata:
        hit = self.cached(issuer)
        if hit is not None:
            return hit
        # One outbound fetch per issuer even when many logins race: the losers
        # re-check the cache after taking the lock.
        async with self._lock_for(issuer):
            hit = self.cached(issuer)
            if hit is not None:
                return hit
            metadata = await self._fetch(issuer)
            if len(self._entries) >= MAX_CACHE_ENTRIES:
                self._entries.clear()
            self._entries[issuer] = (time.monotonic(), metadata)
            return metadata

    async def _fetch(self, issuer: str) -> ProviderMetadata:
        url = discovery_url(issuer)
        try:
            if self._client is not None:
                response = await self._client.get(url, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as c:
                    response = await c.get(url)
        except httpx.HTTPError as exc:
            raise DiscoveryError(
                f"Could not reach the identity provider's discovery endpoint for {issuer}: "
                f"{type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise DiscoveryError(
                f"Identity provider discovery for {issuer} returned HTTP {response.status_code}."
            )
        if len(response.content) > MAX_DOCUMENT_BYTES:
            raise DiscoveryError(f"Discovery document for {issuer} is implausibly large.")
        try:
            document = response.json()
        except ValueError as exc:
            raise DiscoveryError(f"Discovery document for {issuer} is not valid JSON.") from exc
        if not isinstance(document, dict):
            raise DiscoveryError(f"Discovery document for {issuer} is not a JSON object.")
        metadata = parse_metadata(issuer, document)
        get_logger().info(
            "identity.discovery.loaded",
            issuer=issuer,
            has_end_session=metadata.end_session_endpoint is not None,
        )
        return metadata

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()


_cache: Optional[DiscoveryCache] = None


def get_discovery_cache() -> DiscoveryCache:
    global _cache
    if _cache is None:
        _cache = DiscoveryCache()
    return _cache


def set_discovery_cache(cache: DiscoveryCache) -> None:
    global _cache
    _cache = cache


def clear_discovery_cache() -> None:
    global _cache
    if _cache is not None:
        _cache.clear()
    _cache = None
