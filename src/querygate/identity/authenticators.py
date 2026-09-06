"""Turning a browser cookie or an issued token into the same `Principal`.

`core/auth.Authenticator` stays exactly what it was — a *synchronous* protocol
over a bearer token — because both transports call it synchronously and both
`ApiKeyAuthenticator` and `JwtAuthenticator` can answer without I/O. Session and
device credentials cannot: they are looked up in a store that is async by
design. So they resolve through this module's `AsyncCredentialAuthenticator`
protocol, which the transports await *before* falling back to the sync chain,
rather than by making every existing authenticator async.

The authority rule is the same for both, and it is the important part: a
credential's scopes are **re-derived from the live `IdentityMappingStore` on
every request**, from the claims the IdP asserted at login. Nothing here trusts
a scope list that was frozen earlier:

* a session holds claims, never scopes;
* an issued device token holds a scope *ceiling* fixed by its human approver,
  and the effective set is that ceiling intersected with what the approver's
  claims map to right now.

So revoking a group in identity.yaml and reloading takes effect on the next
request for browser sessions and CLI tokens alike — it does not wait for a
logout or a token expiry.
"""

from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Optional, Protocol

from querygate.core.auth import Principal
from querygate.core.logging import get_logger
from querygate.identity.config_store import IdentityConfigStore, get_identity_store
from querygate.identity.device import IssuedTokenStore, get_issued_token_store
from querygate.identity.sessions import (
    DEVICE_TOKEN_PREFIX,
    SESSION_TOKEN_PREFIX,
    SessionStore,
    configured_idle_timeout,
    get_session_store,
    token_digest,
)

# nosec B105: an auth-method *label* recorded on the Principal and in the
# audit event, not a credential.
SESSION_AUTH_METHOD = "sso_session"
DEVICE_TOKEN_AUTH_METHOD = "sso_device_token"  # nosec B105


class AsyncCredentialAuthenticator(Protocol):
    """An `Authenticator` for credentials whose lookup needs to await."""

    async def authenticate(self, credential: Optional[str]) -> Optional[Principal]: ...


class CsrfMismatchError(Exception):
    """A cookie-authenticated request arrived without a matching CSRF token."""


class SessionCookieAuthenticator:
    """Resolves an opaque session token into the signed-in person.

    Requires a matching CSRF token on **every** request, not just unsafe
    methods. QueryGate's UIs are `fetch`-driven and always send the header, so
    the strict rule costs nothing and removes the class of bug where a new
    read endpoint is quietly reachable cross-site because someone judged it
    "safe". A cookie without the header is not a weaker credential — it is not
    a credential.
    """

    def __init__(
        self,
        *,
        session_store: Optional[SessionStore] = None,
        identity_store: Optional[IdentityConfigStore] = None,
        idle_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._session_store = session_store
        self._identity_store = identity_store
        # None means "read identity.yaml on each request". Capturing it at
        # construction would freeze a hot-reloaded timeout for the life of the
        # process, since this object is built once when the app is created.
        self._idle_timeout_override = idle_timeout_seconds

    @property
    def _idle_timeout(self) -> float:
        if self._idle_timeout_override is not None:
            return self._idle_timeout_override
        return configured_idle_timeout()

    @property
    def sessions(self) -> SessionStore:
        return self._session_store or get_session_store()

    @property
    def identity(self) -> IdentityConfigStore:
        return self._identity_store or get_identity_store()

    async def authenticate(
        self, credential: Optional[str], *, csrf_token: Optional[str] = None
    ) -> Optional[Principal]:
        if not credential or not credential.startswith(SESSION_TOKEN_PREFIX):
            return None
        session = await self.sessions.get(token_digest(credential))
        if session is None:
            return None
        now = datetime.now(timezone.utc)
        if session.is_expired(now, idle_timeout=self._idle_timeout):
            await self.sessions.delete(session.session_digest)
            return None
        if not csrf_token or not hmac.compare_digest(csrf_token, session.csrf_token):
            raise CsrfMismatchError(
                "This request is missing its CSRF token. Reload the page and try again."
            )
        await self.sessions.touch(session.session_digest, now)
        return Principal(
            subject=session.subject,
            scopes=self.identity.mapping.scopes_for(session.provider_id, session.claims),
            claims=dict(session.claims),
            auth_method=SESSION_AUTH_METHOD,
        )


class DeviceTokenAuthenticator:
    """Resolves a QueryGate-issued `qgd_` bearer token (see `identity/device.py`)."""

    def __init__(
        self,
        *,
        token_store: Optional[IssuedTokenStore] = None,
        identity_store: Optional[IdentityConfigStore] = None,
    ) -> None:
        self._token_store = token_store
        self._identity_store = identity_store

    @property
    def tokens(self) -> IssuedTokenStore:
        return self._token_store or get_issued_token_store()

    @property
    def identity(self) -> IdentityConfigStore:
        return self._identity_store or get_identity_store()

    async def authenticate(self, credential: Optional[str]) -> Optional[Principal]:
        if not credential or not credential.startswith(DEVICE_TOKEN_PREFIX):
            return None
        token = await self.tokens.get(token_digest(credential))
        if token is None:
            return None
        ceiling = frozenset(token.scopes)
        live = self.identity.mapping.scopes_for(token.provider_id, token.approver_claims)
        effective = ceiling & live
        if ceiling and not effective:
            # The approver's authority was revoked after the token was minted.
            # Keep the token usable as an *identity* only if it was minted with
            # no scopes to begin with; otherwise it is now inert, and saying so
            # once in the log is more useful than a silent 403 later.
            get_logger().info(
                "identity.device_token.scopes_revoked",
                subject=token.subject,
                client_name=token.client_name,
            )
        return Principal(
            subject=token.subject,
            scopes=effective,
            claims=dict(token.approver_claims),
            auth_method=DEVICE_TOKEN_AUTH_METHOD,
        )


class CompositeAsyncAuthenticator:
    """Tries each async authenticator in order, first match wins.

    The async sibling of `core/auth.CompositeAuthenticator`, and it composes for
    the same reason: a deployment can accept a browser session *and* a CLI token
    on the same endpoint without either transport knowing which one answered.
    """

    def __init__(self, authenticators: list[AsyncCredentialAuthenticator]) -> None:
        self._authenticators = authenticators

    async def authenticate(self, credential: Optional[str]) -> Optional[Principal]:
        for authenticator in self._authenticators:
            principal = await authenticator.authenticate(credential)
            if principal is not None:
                return principal
        return None
