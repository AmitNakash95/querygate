"""RFC 8628 device authorization grant — an SSO identity for CLI and API callers.

A person who has signed in through SSO in a browser can hand that identity to a
tool that has no browser: `querygate login` prints a short user code, the person
approves it in the admin UI, and the tool receives a short-lived QueryGate
access token bound to **their** subject.

The property that matters is that this can only ever *narrow*: the issued token
carries the intersection of what the tool asked for and what the approver
actually holds at approval time (`grant_scopes`). There is no path here by which
a device grant produces authority its approver does not have, and no path by
which a pending grant becomes a token without an authenticated human action.

Both stores are async Protocols from the start, for the same reason the session
stores are: a multi-replica deployment needs the shared-state variant, and the
call sites must not have to change when it arrives. What a caller holds is an
opaque token; what a store holds is its SHA-256.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Protocol

import pydantic as pyd

from querygate.identity.sessions import DEVICE_TOKEN_PREFIX, new_opaque_token, token_digest

DEVICE_CODE_PREFIX = "qgdc_"
# Excludes vowels (no accidental words) and every character pair people confuse
# reading a code aloud: 0/O, 1/I/L, 5/S, 2/Z, 8/B.
USER_CODE_ALPHABET = "CDFGHJKMNPQRTVWXY3467"
USER_CODE_LENGTH = 8
USER_CODE_GROUP = 4
DEFAULT_DEVICE_CODE_TTL_SECONDS = 600.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MAX_PENDING_GRANTS = 4096
MAX_ISSUED_TOKENS = 20000
MAX_CLIENT_NAME_LENGTH = 64

GrantStatus = Literal["pending", "approved", "denied"]


class DeviceGrantError(Exception):
    """A device-grant step failed. `error` is the RFC 8628 error code."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message


def new_user_code() -> str:
    body = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    return "-".join(body[i : i + USER_CODE_GROUP] for i in range(0, len(body), USER_CODE_GROUP))


def normalize_user_code(value: str) -> str:
    """Accept what a person actually types: any case, with or without dashes."""
    cleaned = "".join(ch for ch in (value or "").upper() if ch.isalnum())
    return "-".join(
        cleaned[i : i + USER_CODE_GROUP] for i in range(0, len(cleaned), USER_CODE_GROUP)
    )


class DeviceGrant(pyd.BaseModel):
    """One pending or resolved device authorization."""

    device_code_digest: str
    user_code: str
    client_name: str
    requested_scopes: List[str] = pyd.Field(default_factory=list)
    status: GrantStatus = "pending"
    subject: str = ""
    provider_id: str = ""
    granted_scopes: List[str] = pyd.Field(default_factory=list)
    # The approver's claim set, carried through to the issued token so the
    # token's authority can be re-derived from the *current* mapping on every
    # request rather than frozen at approval time.
    approver_claims: Dict[str, Any] = pyd.Field(default_factory=dict)
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    created_at: datetime
    expires_at: datetime
    last_polled_at: Optional[datetime] = None

    model_config = pyd.ConfigDict(extra="forbid")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class IssuedToken(pyd.BaseModel):
    """A QueryGate-minted bearer token standing in for a human's SSO identity."""

    token_digest: str
    subject: str
    provider_id: str
    client_name: str
    # The ceiling this token can ever exercise: what the approver held AND what
    # the client asked for, fixed at approval. `authenticators.py` intersects it
    # again with the approver's live mapped scopes on every request, so the
    # token can only ever shrink from here — never grow.
    scopes: List[str] = pyd.Field(default_factory=list)
    approver_claims: Dict[str, Any] = pyd.Field(default_factory=dict)
    created_at: datetime
    expires_at: datetime

    model_config = pyd.ConfigDict(extra="forbid")

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return (now or datetime.now(timezone.utc)) >= self.expires_at


class PublicDeviceGrant(pyd.BaseModel):
    """What the approving human is shown before they approve.

    No device code, no token — only what the tool asked for and who it says it
    is, so a person can make a real decision without the page ever holding a
    credential.
    """

    user_code: str
    client_name: str
    requested_scopes: List[str] = pyd.Field(default_factory=list)
    status: GrantStatus
    expires_at: datetime

    model_config = pyd.ConfigDict(extra="forbid")


class PublicIssuedToken(pyd.BaseModel):
    """A token's metadata, for the "your active tokens" list. Never the token."""

    token_digest: str
    subject: str
    client_name: str
    scopes: List[str] = pyd.Field(default_factory=list)
    created_at: datetime
    expires_at: datetime

    model_config = pyd.ConfigDict(extra="forbid")


class DeviceGrantStore(Protocol):
    async def put(self, grant: DeviceGrant) -> None: ...

    async def by_device_code(self, device_code_digest: str) -> Optional[DeviceGrant]: ...

    async def by_user_code(self, user_code: str) -> Optional[DeviceGrant]: ...

    async def delete(self, device_code_digest: str) -> None: ...


class IssuedTokenStore(Protocol):
    async def put(self, token: IssuedToken) -> None: ...

    async def get(self, token_digest: str) -> Optional[IssuedToken]: ...

    async def delete(self, token_digest: str) -> None: ...

    async def delete_for_subject(self, subject: str) -> int: ...

    async def list_for_subject(self, subject: str) -> List[IssuedToken]: ...


class InProcessDeviceGrantStore:
    def __init__(self, max_entries: int = MAX_PENDING_GRANTS) -> None:
        self._grants: Dict[str, DeviceGrant] = {}
        self._by_user_code: Dict[str, str] = {}
        self._max_entries = max_entries

    async def put(self, grant: DeviceGrant) -> None:
        self._prune()
        if len(self._grants) >= self._max_entries:
            oldest = min(self._grants.values(), key=lambda g: g.created_at)
            await self.delete(oldest.device_code_digest)
        self._grants[grant.device_code_digest] = grant
        self._by_user_code[grant.user_code] = grant.device_code_digest

    async def by_device_code(self, device_code_digest: str) -> Optional[DeviceGrant]:
        grant = self._grants.get(device_code_digest)
        if grant is not None and grant.is_expired():
            await self.delete(device_code_digest)
            return None
        return grant

    async def by_user_code(self, user_code: str) -> Optional[DeviceGrant]:
        digest = self._by_user_code.get(user_code)
        return await self.by_device_code(digest) if digest else None

    async def delete(self, device_code_digest: str) -> None:
        grant = self._grants.pop(device_code_digest, None)
        if grant is not None:
            self._by_user_code.pop(grant.user_code, None)

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        for digest in [d for d, g in self._grants.items() if g.is_expired(now)]:
            grant = self._grants.pop(digest, None)
            if grant is not None:
                self._by_user_code.pop(grant.user_code, None)

    def clear(self) -> None:
        self._grants.clear()
        self._by_user_code.clear()


class InProcessIssuedTokenStore:
    def __init__(self, max_entries: int = MAX_ISSUED_TOKENS) -> None:
        self._tokens: Dict[str, IssuedToken] = {}
        self._max_entries = max_entries

    async def put(self, token: IssuedToken) -> None:
        self._prune()
        if len(self._tokens) >= self._max_entries:
            oldest = min(self._tokens.values(), key=lambda t: t.expires_at)
            self._tokens.pop(oldest.token_digest, None)
        self._tokens[token.token_digest] = token

    async def get(self, token_digest: str) -> Optional[IssuedToken]:
        token = self._tokens.get(token_digest)
        if token is None:
            return None
        if token.is_expired():
            self._tokens.pop(token_digest, None)
            return None
        return token

    async def delete(self, token_digest: str) -> None:
        self._tokens.pop(token_digest, None)

    async def delete_for_subject(self, subject: str) -> int:
        digests = [d for d, t in self._tokens.items() if t.subject == subject]
        for digest in digests:
            self._tokens.pop(digest, None)
        return len(digests)

    async def list_for_subject(self, subject: str) -> List[IssuedToken]:
        self._prune()
        return sorted(
            (t for t in self._tokens.values() if t.subject == subject),
            key=lambda t: t.created_at,
        )

    def _prune(self) -> None:
        now = datetime.now(timezone.utc)
        for digest in [d for d, t in self._tokens.items() if t.is_expired(now)]:
            self._tokens.pop(digest, None)

    def clear(self) -> None:
        self._tokens.clear()


_in_process_grants = InProcessDeviceGrantStore()
_in_process_tokens = InProcessIssuedTokenStore()
_grant_store: Optional[DeviceGrantStore] = None
_token_store: Optional[IssuedTokenStore] = None


def get_device_grant_store() -> DeviceGrantStore:
    return _grant_store or _in_process_grants


def get_issued_token_store() -> IssuedTokenStore:
    return _token_store or _in_process_tokens


def set_device_grant_store(store: Optional[DeviceGrantStore]) -> None:
    global _grant_store
    _grant_store = store


def set_issued_token_store(store: Optional[IssuedTokenStore]) -> None:
    global _token_store
    _token_store = store


def clear_in_process_device_state() -> None:
    _in_process_grants.clear()
    _in_process_tokens.clear()


async def start_device_authorization(
    *,
    client_name: str,
    requested_scopes: List[str],
    ttl_seconds: float = DEFAULT_DEVICE_CODE_TTL_SECONDS,
    interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> tuple[str, DeviceGrant]:
    """Create a pending grant. Returns (device_code, grant) — the code once."""
    name = (client_name or "unnamed client").strip()[:MAX_CLIENT_NAME_LENGTH]
    device_code = new_opaque_token(DEVICE_CODE_PREFIX)
    now = datetime.now(timezone.utc)
    grant = DeviceGrant(
        device_code_digest=token_digest(device_code),
        user_code=new_user_code(),
        client_name=name,
        requested_scopes=sorted(set(requested_scopes)),
        interval_seconds=interval_seconds,
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
    )
    await get_device_grant_store().put(grant)
    return device_code, grant


async def approve_device_grant(
    *,
    user_code: str,
    approver_subject: str,
    approver_scopes: frozenset[str],
    provider_id: str,
    approver_claims: Optional[Dict[str, Any]] = None,
) -> DeviceGrant:
    """Bind a pending grant to the approving human, narrowed to their authority."""
    store = get_device_grant_store()
    grant = await store.by_user_code(normalize_user_code(user_code))
    if grant is None:
        raise DeviceGrantError("invalid_user_code", "That code is not valid or has expired.")
    if grant.status != "pending":
        raise DeviceGrantError("already_resolved", "That code has already been used.")
    # Intersection, always. A tool asking for a scope its approver does not hold
    # simply does not receive it; asking for nothing receives the approver's
    # scopes, which is the "act as me" case the CLI actually wants.
    requested = set(grant.requested_scopes) if grant.requested_scopes else set(approver_scopes)
    granted = sorted(requested & set(approver_scopes))
    approved = grant.model_copy(
        update={
            "status": "approved",
            "subject": approver_subject,
            "provider_id": provider_id,
            "granted_scopes": granted,
            "approver_claims": dict(approver_claims or {}),
        }
    )
    await store.put(approved)
    return approved


async def deny_device_grant(*, user_code: str) -> DeviceGrant:
    store = get_device_grant_store()
    grant = await store.by_user_code(normalize_user_code(user_code))
    if grant is None:
        raise DeviceGrantError("invalid_user_code", "That code is not valid or has expired.")
    denied = grant.model_copy(update={"status": "denied"})
    await store.put(denied)
    return denied


async def redeem_device_code(
    *, device_code: str, token_ttl_seconds: float
) -> tuple[str, IssuedToken]:
    """Exchange an approved device code for an access token. Single use."""
    store = get_device_grant_store()
    digest = token_digest(device_code)
    grant = await store.by_device_code(digest)
    if grant is None:
        raise DeviceGrantError("expired_token", "This sign-in request has expired. Start again.")
    now = datetime.now(timezone.utc)
    if grant.status == "denied":
        await store.delete(digest)
        raise DeviceGrantError("access_denied", "The sign-in request was denied.")
    if grant.status != "approved":
        # RFC 8628 §3.5's back-off applies to *waiting*, which is the only state
        # a client can spin on. An already-approved grant is served immediately:
        # rate-limiting it would add latency to a completed human decision
        # without bounding anything a caller could abuse.
        polled_at = grant.last_polled_at
        await store.put(grant.model_copy(update={"last_polled_at": now}))
        if polled_at is not None and (now - polled_at).total_seconds() < grant.interval_seconds:
            raise DeviceGrantError("slow_down", "Polling too quickly; wait before retrying.")
        raise DeviceGrantError("authorization_pending", "Waiting for approval in the browser.")

    # Approved: burn the grant before minting, so one device code can never
    # yield two tokens even if two polls race.
    await store.delete(digest)
    access_token = new_opaque_token(DEVICE_TOKEN_PREFIX)
    token = IssuedToken(
        token_digest=token_digest(access_token),
        subject=grant.subject,
        provider_id=grant.provider_id,
        client_name=grant.client_name,
        scopes=list(grant.granted_scopes),
        approver_claims=dict(grant.approver_claims),
        created_at=now,
        expires_at=now + timedelta(seconds=token_ttl_seconds),
    )
    await get_issued_token_store().put(token)
    return access_token, token
