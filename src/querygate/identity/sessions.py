"""Browser sessions and in-flight login attempts.

Two short-lived, server-side stores back the SSO surface. Both are defined as
**async Protocols from the start** — per CLAUDE.md's store-conversion gotcha,
so the Redis-backed siblings (`identity/redis_sessions.py`, for a multi-replica
deployment) needed no later `def`→`async def` conversion at their call sites.

`LoginFlowStore` holds one pending authorization request: its CSRF `state`, the
ID-token `nonce`, and the PKCE `code_verifier`. It is single-use and short-TTL —
`consume()` deletes as it reads, so a replayed callback finds nothing.

`SessionStore` holds an authenticated person. What a browser holds is an opaque
random token; what the store holds is its SHA-256, so a dump of the session
store (or a Redis snapshot, or a log line) yields nothing a browser could
present. The session records the caller's *claims*, not their resolved scopes —
authority is re-derived from `IdentityMappingStore` on every request, so
tightening a mapping takes effect on the next request rather than at the next
logout.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Protocol

import pydantic as pyd

# Opaque-credential prefixes. They exist so `identity/authenticators.py` can
# route a presented credential to the right resolver without trying every one,
# and so an operator seeing a value in a bug report knows what it is.
# nosec B105 x2: these are opaque-credential *namespace prefixes*, not
# secrets. They exist so a presented credential can be routed to the right
# resolver without trying every one, and so an operator seeing a value in a
# bug report knows what kind of thing it is. The secret is the 256 bits of
# `secrets.token_urlsafe` that follow.
SESSION_TOKEN_PREFIX = "qgs_"  # nosec B105
DEVICE_TOKEN_PREFIX = "qgd_"  # nosec B105
# 32 bytes = 256 bits of entropy, urlsafe-base64 encoded.
TOKEN_ENTROPY_BYTES = 32
# A real ID token's claim set is 1-4 KB. Refusing anything far larger keeps an
# unbounded IdP assertion out of the session store (and out of Redis).
MAX_SESSION_CLAIMS_BYTES = 16 * 1024
# Claims that are single-use artifacts of the login exchange itself, or bind to
# a token QueryGate does not keep. Storing them would add risk and no value.
_STRIPPED_CLAIMS = frozenset({"nonce", "at_hash", "c_hash", "s_hash", "cnf"})


def new_opaque_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def token_digest(token: str) -> str:
    """The stored form of an opaque token. Never reversible to the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sanitize_claims(claims: Mapping[str, Any]) -> Dict[str, Any]:
    """The claim subset a session may retain, with login artifacts removed."""
    kept = {key: value for key, value in claims.items() if key not in _STRIPPED_CLAIMS}
    encoded = json.dumps(kept, default=str)
    if len(encoded.encode("utf-8")) > MAX_SESSION_CLAIMS_BYTES:
        raise ValueError(
            "The identity provider returned a claim set larger than QueryGate will "
            f"retain in a session ({MAX_SESSION_CLAIMS_BYTES} bytes). Trim the token's "
            "claims (Entra ID: use group filtering rather than emitting every group)."
        )
    return kept


class LoginFlow(pyd.BaseModel):
    """One in-flight authorization-code request."""

    flow_id: str
    provider_id: str
    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    return_to: str
    created_at: datetime
    expires_at: datetime

    model_config = pyd.ConfigDict(extra="forbid")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class SsoSession(pyd.BaseModel):
    """One authenticated human's browser session."""

    session_digest: str
    subject: str
    provider_id: str
    auth_method: str
    csrf_token: str
    display_name: str = ""
    email: str = ""
    claims: Dict[str, Any] = pyd.Field(default_factory=dict)
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime

    model_config = pyd.ConfigDict(extra="forbid")

    def is_expired(self, now: Optional[datetime] = None, *, idle_timeout: float = 0.0) -> bool:
        current = now or datetime.now(timezone.utc)
        if current >= self.expires_at:
            return True
        if idle_timeout > 0:
            return (current - self.last_seen_at).total_seconds() > idle_timeout
        return False


def configured_idle_timeout() -> float:
    """The deployment's session idle timeout, read live from identity.yaml.

    Read here rather than captured when a store or an authenticator is built:
    identity.yaml hot-reloads, and a value frozen at app-build time would keep
    a stale timeout for the life of the process. Imported lazily to keep
    `sessions.py` free of a module-level dependency on the config store.
    """
    from querygate.identity.config_store import get_identity_store

    return get_identity_store().settings.session_idle_timeout_seconds


class LoginFlowStore(Protocol):
    async def put(self, flow: LoginFlow) -> None: ...

    async def consume(self, flow_id: str) -> Optional[LoginFlow]:
        """Return the flow and delete it. A second call returns None."""
        ...


class SessionStore(Protocol):
    async def create(self, session: SsoSession) -> None: ...

    async def get(self, session_digest: str) -> Optional[SsoSession]: ...

    async def touch(self, session_digest: str, seen_at: datetime) -> None: ...

    async def delete(self, session_digest: str) -> None: ...

    async def delete_for_subject(self, subject: str) -> int:
        """Revoke every session for one person. Returns how many were removed."""
        ...


class InProcessLoginFlowStore:
    """Single-replica login-flow store.

    Bounded and self-pruning: a login flow that is started and never completed
    (a user who closes the tab at the IdP) expires on its own, and an attempt to
    flood the store evicts oldest-first rather than growing without limit.
    """

    def __init__(self, max_entries: int = 4096) -> None:
        self._flows: Dict[str, LoginFlow] = {}
        self._max_entries = max_entries

    async def put(self, flow: LoginFlow) -> None:
        self._prune()
        if len(self._flows) >= self._max_entries:
            oldest = min(self._flows.values(), key=lambda f: f.created_at)
            self._flows.pop(oldest.flow_id, None)
        self._flows[flow.flow_id] = flow

    async def consume(self, flow_id: str) -> Optional[LoginFlow]:
        flow = self._flows.pop(flow_id, None)
        if flow is None or flow.is_expired():
            return None
        return flow

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        for flow_id in [fid for fid, flow in self._flows.items() if flow.is_expired(now)]:
            self._flows.pop(flow_id, None)

    def clear(self) -> None:
        self._flows.clear()


class InProcessSessionStore:
    """Single-replica session store.

    A deployment running more than one replica must use the Redis-backed
    variant (`identity/redis_sessions.py`) or sticky sessions; otherwise a
    browser authenticated on replica A is anonymous on replica B. This is the
    same shared-state consideration `deploy/HA_DR.md` records for the
    in-process concurrency limiter and quota store.
    """

    def __init__(
        self, max_entries: int = 20000, idle_timeout_seconds: Optional[float] = None
    ) -> None:
        self._sessions: Dict[str, SsoSession] = {}
        self._max_entries = max_entries
        # None (the default) means "whatever identity.yaml currently says", so a
        # hot-reloaded timeout takes effect immediately. A number is an explicit
        # override, used by tests that need a fixed value.
        self._idle_timeout_override = idle_timeout_seconds

    @property
    def _idle_timeout(self) -> float:
        if self._idle_timeout_override is not None:
            return self._idle_timeout_override
        return configured_idle_timeout()

    async def create(self, session: SsoSession) -> None:
        self._prune()
        if len(self._sessions) >= self._max_entries:
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen_at)
            self._sessions.pop(oldest.session_digest, None)
        self._sessions[session.session_digest] = session

    async def get(self, session_digest: str) -> Optional[SsoSession]:
        session = self._sessions.get(session_digest)
        if session is None:
            return None
        if session.is_expired(idle_timeout=self._idle_timeout):
            self._sessions.pop(session_digest, None)
            return None
        return session

    async def touch(self, session_digest: str, seen_at: datetime) -> None:
        session = self._sessions.get(session_digest)
        if session is not None:
            self._sessions[session_digest] = session.model_copy(update={"last_seen_at": seen_at})

    async def delete(self, session_digest: str) -> None:
        self._sessions.pop(session_digest, None)

    async def delete_for_subject(self, subject: str) -> int:
        digests = [d for d, s in self._sessions.items() if s.subject == subject]
        for digest in digests:
            self._sessions.pop(digest, None)
        return len(digests)

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        stale = [
            digest
            for digest, session in self._sessions.items()
            if session.is_expired(now, idle_timeout=self._idle_timeout)
        ]
        for digest in stale:
            self._sessions.pop(digest, None)

    def clear(self) -> None:
        self._sessions.clear()

    def subjects(self) -> List[str]:
        return sorted({session.subject for session in self._sessions.values()})


_login_flow_store: Optional[LoginFlowStore] = None
_session_store: Optional[SessionStore] = None
_in_process_login_flows = InProcessLoginFlowStore()
_in_process_sessions = InProcessSessionStore()


def get_login_flow_store() -> LoginFlowStore:
    return _login_flow_store or _in_process_login_flows


def get_session_store() -> SessionStore:
    return _session_store or _in_process_sessions


def set_login_flow_store(store: Optional[LoginFlowStore]) -> None:
    global _login_flow_store
    _login_flow_store = store


def set_session_store(store: Optional[SessionStore]) -> None:
    global _session_store
    _session_store = store


def configure_in_process_idle_timeout(seconds: float) -> None:
    _in_process_sessions._idle_timeout = seconds


def clear_in_process_identity_state() -> None:
    """Reset the single-replica stores — used by the test fixture and by a
    lifespan shutdown, the same discipline `in_process_limiter().clear()`
    follows for concurrency state."""
    _in_process_login_flows.clear()
    _in_process_sessions.clear()


def monotonic_now() -> float:
    return time.monotonic()
