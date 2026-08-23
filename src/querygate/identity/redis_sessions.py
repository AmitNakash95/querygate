"""Cross-replica SSO state: sessions, login flows, device grants, issued tokens.

The distributed siblings of the in-process stores in `identity/sessions.py` and
`identity/device.py`. Without them a browser authenticated on replica A is
anonymous on replica B — the same shared-state gap `deploy/HA_DR.md` records for
the in-process concurrency limiter and quota store, but with a worse symptom: a
person is randomly signed out rather than granted a slightly generous budget.

**Deliberately no Lua anywhere in this module.** CLAUDE.md records two traps a
scripted implementation would walk straight into, both invisible under
fakeredis: `cjson.decode` + `cjson.encode` turns every empty JSON array into an
empty object on real Redis (item 195 phase 2), and a script reaching a key it
did not declare in `KEYS` is a Cluster violation no single-node test can detect
(item 192). A session record is a JSON document full of arrays — `scopes`,
`groups`, whatever the IdP nested inside `claims` — so it is exactly the payload
the first trap destroys.

So this module takes the structural way around, which CLAUDE.md names directly:
**keep the document opaque and never decode it server-side**, and hold every
mutable field in its own key a primitive command can update. `last_seen_at` —
the only field that changes after creation — lives in a separate string key
written with `SET`, so refreshing a session's idle clock is one small write
rather than a read-modify-write of the document.

Every command here touches exactly one key, so nothing can raise `CROSSSLOT`,
and single-use semantics come from `GETDEL` (Redis >= 6.2) rather than a
check-then-delete that a second replica could race.

Failure posture is **fail-closed**, unlike the concurrency limiter's optional
fail-open: if Redis is unreachable, the caller is simply not signed in. An
authentication store that fails open is not an authentication store.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from redis.exceptions import RedisError

from querygate.core.logging import get_logger
from querygate.identity.device import DeviceGrant, IssuedToken
from querygate.identity.sessions import LoginFlow, SsoSession

# One hash tag per logical record, so a record's own keys share a slot even
# though no single command spans two of them today.
SESSION_KEY = "qgsso:sess:{{{digest}}}"
SESSION_SEEN_KEY = "qgsso:seen:{{{digest}}}"
SUBJECT_KEY = "qgsso:subj:{{{subject}}}"
FLOW_KEY = "qgsso:flow:{{{flow_id}}}"
GRANT_KEY = "qgsso:grant:{{{digest}}}"
USER_CODE_KEY = "qgsso:ucode:{{{user_code}}}"
# nosec B105 x2: Redis key *templates*, not credentials. What they key on is a
# SHA-256 digest supplied at call time; the token itself is never stored.
TOKEN_KEY = "qgsso:tok:{{{digest}}}"  # nosec B105
TOKEN_SUBJECT_KEY = "qgsso:toksubj:{{{subject}}}"  # nosec B105

# Upper bound on how long an index set may outlive the records it points at.
INDEX_TTL_SECONDS = 60 * 60 * 24 * 31


def _ttl(expires_at: datetime, *, minimum: int = 1) -> int:
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    return max(minimum, int(remaining))


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _decode(raw: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Parse a stored document. Never re-encoded — see the module docstring."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


class RedisSessionStore:
    """Browser sessions, shared by every replica."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def create(self, session: SsoSession) -> None:
        # The document is written ONCE and never decoded by Redis. `last_seen_at`
        # is carried separately below precisely so this value never needs a
        # read-modify-write, which is what would reintroduce the cjson hazard.
        ttl = _ttl(session.expires_at)
        await self._redis.set(
            SESSION_KEY.format(digest=session.session_digest), session.model_dump_json(), ex=ttl
        )
        await self._redis.set(
            SESSION_SEEN_KEY.format(digest=session.session_digest),
            str(session.last_seen_at.timestamp()),
            ex=ttl,
        )
        subject_key = SUBJECT_KEY.format(subject=session.subject)
        await self._redis.sadd(subject_key, session.session_digest)
        await self._redis.expire(subject_key, INDEX_TTL_SECONDS)

    async def get(self, session_digest: str) -> Optional[SsoSession]:
        from querygate.identity.sessions import configured_idle_timeout

        try:
            raw = await self._redis.get(SESSION_KEY.format(digest=session_digest))
            seen_raw = await self._redis.get(SESSION_SEEN_KEY.format(digest=session_digest))
        except RedisError as exc:
            # Fail closed: an unreachable session store means nobody is signed in.
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return None
        payload = _decode(raw)
        if payload is None:
            return None
        if seen_raw is not None:
            try:
                payload["last_seen_at"] = datetime.fromtimestamp(
                    float(_text(seen_raw)), tz=timezone.utc
                ).isoformat()
            except (ValueError, TypeError, OSError):
                pass
        try:
            session = SsoSession.model_validate(payload)
        except ValueError:
            return None
        if session.is_expired(idle_timeout=configured_idle_timeout()):
            await self.delete(session_digest)
            return None
        return session

    async def touch(self, session_digest: str, seen_at: datetime) -> None:
        try:
            ttl = await self._redis.ttl(SESSION_KEY.format(digest=session_digest))
            # A single small write. No document is read, decoded, or rewritten,
            # so refreshing the idle clock cannot corrupt the claim set.
            await self._redis.set(
                SESSION_SEEN_KEY.format(digest=session_digest),
                str(seen_at.timestamp()),
                ex=int(ttl) if ttl and int(ttl) > 0 else None,
            )
        except RedisError as exc:
            get_logger().warning("identity.redis_sessions.touch_failed", error=type(exc).__name__)

    async def delete(self, session_digest: str) -> None:
        try:
            raw = await self._redis.get(SESSION_KEY.format(digest=session_digest))
            await self._redis.delete(SESSION_KEY.format(digest=session_digest))
            await self._redis.delete(SESSION_SEEN_KEY.format(digest=session_digest))
            payload = _decode(raw)
            if payload and payload.get("subject"):
                await self._redis.srem(
                    SUBJECT_KEY.format(subject=payload["subject"]), session_digest
                )
        except RedisError as exc:
            get_logger().warning("identity.redis_sessions.delete_failed", error=type(exc).__name__)

    async def delete_for_subject(self, subject: str) -> int:
        subject_key = SUBJECT_KEY.format(subject=subject)
        try:
            members = await self._redis.smembers(subject_key)
        except RedisError as exc:
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return 0
        removed = 0
        for member in members or []:
            digest = _text(member)
            # One key per command: a multi-key DEL across these would be a
            # CROSSSLOT error on a real Redis Cluster.
            if await self._redis.delete(SESSION_KEY.format(digest=digest)):
                removed += 1
            await self._redis.delete(SESSION_SEEN_KEY.format(digest=digest))
        await self._redis.delete(subject_key)
        return removed


class RedisLoginFlowStore:
    """In-flight logins, single-use across replicas.

    `consume` uses `GETDEL`, so two replicas racing the same callback cannot
    both see the flow — which is why a check-then-delete would be wrong here
    rather than merely untidy.
    """

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def put(self, flow: LoginFlow) -> None:
        await self._redis.set(
            FLOW_KEY.format(flow_id=flow.flow_id), flow.model_dump_json(), ex=_ttl(flow.expires_at)
        )

    async def consume(self, flow_id: str) -> Optional[LoginFlow]:
        try:
            raw = await self._redis.getdel(FLOW_KEY.format(flow_id=flow_id))
        except RedisError as exc:
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return None
        payload = _decode(raw)
        if payload is None:
            return None
        try:
            flow = LoginFlow.model_validate(payload)
        except ValueError:
            return None
        return None if flow.is_expired() else flow


class RedisDeviceGrantStore:
    """Pending device authorizations, so any replica can serve the poll."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def put(self, grant: DeviceGrant) -> None:
        ttl = _ttl(grant.expires_at)
        await self._redis.set(
            GRANT_KEY.format(digest=grant.device_code_digest), grant.model_dump_json(), ex=ttl
        )
        await self._redis.set(
            USER_CODE_KEY.format(user_code=grant.user_code), grant.device_code_digest, ex=ttl
        )

    async def by_device_code(self, device_code_digest: str) -> Optional[DeviceGrant]:
        try:
            raw = await self._redis.get(GRANT_KEY.format(digest=device_code_digest))
        except RedisError as exc:
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return None
        payload = _decode(raw)
        if payload is None:
            return None
        try:
            grant = DeviceGrant.model_validate(payload)
        except ValueError:
            return None
        if grant.is_expired():
            await self.delete(device_code_digest)
            return None
        return grant

    async def by_user_code(self, user_code: str) -> Optional[DeviceGrant]:
        try:
            digest = await self._redis.get(USER_CODE_KEY.format(user_code=user_code))
        except RedisError:
            return None
        return None if digest is None else await self.by_device_code(_text(digest))

    async def delete(self, device_code_digest: str) -> None:
        raw = await self._redis.get(GRANT_KEY.format(digest=device_code_digest))
        await self._redis.delete(GRANT_KEY.format(digest=device_code_digest))
        payload = _decode(raw)
        if payload and payload.get("user_code"):
            await self._redis.delete(USER_CODE_KEY.format(user_code=payload["user_code"]))


class RedisIssuedTokenStore:
    """Issued device tokens, so a token works against any replica — and so
    revoking one takes effect on all of them at once."""

    def __init__(self, client: Any) -> None:
        self._redis = client

    async def put(self, token: IssuedToken) -> None:
        ttl = _ttl(token.expires_at)
        await self._redis.set(
            TOKEN_KEY.format(digest=token.token_digest), token.model_dump_json(), ex=ttl
        )
        subject_key = TOKEN_SUBJECT_KEY.format(subject=token.subject)
        await self._redis.sadd(subject_key, token.token_digest)
        await self._redis.expire(subject_key, INDEX_TTL_SECONDS)

    async def get(self, token_digest: str) -> Optional[IssuedToken]:
        try:
            raw = await self._redis.get(TOKEN_KEY.format(digest=token_digest))
        except RedisError as exc:
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return None
        payload = _decode(raw)
        if payload is None:
            return None
        try:
            token = IssuedToken.model_validate(payload)
        except ValueError:
            return None
        return None if token.is_expired() else token

    async def delete(self, token_digest: str) -> None:
        raw = await self._redis.get(TOKEN_KEY.format(digest=token_digest))
        await self._redis.delete(TOKEN_KEY.format(digest=token_digest))
        payload = _decode(raw)
        if payload and payload.get("subject"):
            await self._redis.srem(
                TOKEN_SUBJECT_KEY.format(subject=payload["subject"]), token_digest
            )

    async def delete_for_subject(self, subject: str) -> int:
        subject_key = TOKEN_SUBJECT_KEY.format(subject=subject)
        try:
            members = await self._redis.smembers(subject_key)
        except RedisError as exc:
            get_logger().error("identity.redis_sessions.unavailable", error=type(exc).__name__)
            return 0
        removed = 0
        for member in members or []:
            if await self._redis.delete(TOKEN_KEY.format(digest=_text(member))):
                removed += 1
        await self._redis.delete(subject_key)
        return removed

    async def list_for_subject(self, subject: str) -> List[IssuedToken]:
        try:
            members = await self._redis.smembers(TOKEN_SUBJECT_KEY.format(subject=subject))
        except RedisError:
            return []
        tokens: List[IssuedToken] = []
        for member in members or []:
            token = await self.get(_text(member))
            if token is not None:
                tokens.append(token)
        return sorted(tokens, key=lambda t: t.created_at)
