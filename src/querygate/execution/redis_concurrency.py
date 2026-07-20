"""Redis-backed distributed concurrency limiter.

`execution/concurrency.py`'s default `InProcessConcurrencyLimiter` keeps one
`asyncio.Semaphore` per connection id — run N instances behind a load
balancer with `max_concurrency: 8` and the real ceiling against that
database is `8 * N`, not 8, silently defeating the configured guardrail at
exactly the moment it matters (a traffic burst that triggers autoscaling).
`RedisConcurrencyLimiter` enforces one shared budget per connection id
across every instance that points at the same Redis. Implements the same
`ConcurrencyLimiter` shape `InProcessConcurrencyLimiter` does (CLAUDE.md's
"Composable single-purpose interfaces" section) — no changes needed here
when that Protocol was introduced.

Design: each held slot is a member of a per-connection Redis sorted set,
scored by acquisition time. Acquiring is a single Lua script (atomic —
avoids a check-then-set race between instances) that first drops members
older than `lease_seconds` (reclaiming a slot from an instance that crashed
mid-query without releasing it) and then adds the new member only if the
set is under capacity. There's no native blocking primitive for a sorted
set, so waiting for a free slot is short-interval polling, not a blocking
call — bounded by `wait_seconds`, matching the in-process semaphore's
`asyncio.wait_for` timeout semantics.

`enter_queue`/`leave_queue` (TODO.md item 35 phase 2) track *waiters*, not
held slots, with the same sorted-set-plus-lease design: a second pair of
per-connection (and, when a principal is given, per-connection-per-
principal) sorted sets make `querygate_queue_depth` an actual cross-replica
count instead of one process's own local tally, and let
`Policy.max_queue_depth`/`max_queue_depth_per_principal` reject a waiter
before it ever calls `acquire()` — a queue-depth pressure control that,
unlike `max_concurrency`, has no effect until more than one instance shares
this Redis, so it needs the same cross-replica bookkeeping `acquire` already
has.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Optional

from redis.exceptions import RedisError

from querygate.core.logging import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis

# KEYS[1] = per-connection sorted-set key
# ARGV[1] = now (unix seconds, float)
# ARGV[2] = lease_seconds
# ARGV[3] = max_concurrency
# ARGV[4] = this attempt's unique member token
_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - tonumber(ARGV[2]))
local count = redis.call('ZCARD', KEYS[1])
if count < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
    redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) * 2000))
    return 1
else
    return 0
end
"""

# Returned by acquire() when Redis is unreachable and fail_open is True —
# release() recognizes it and skips contacting Redis again rather than
# risking a second failure/log spam on the way out.
_FAIL_OPEN_TOKEN = "__querygate_fail_open__"

# KEYS[1] = global per-connection queue key
# KEYS[2] = per-connection-per-principal queue key (unused, but must still be
#           a valid key name, when ARGV[6] is '0')
# ARGV[1] = now (unix seconds, float)
# ARGV[2] = lease_seconds
# ARGV[3] = max_queue_depth, or -1 for unlimited
# ARGV[4] = max_queue_depth_per_principal, or -1 for unlimited
# ARGV[5] = this waiter's unique member token
# ARGV[6] = '1' if a principal-scoped cap applies, else '0'
_QUEUE_ENTER_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - tonumber(ARGV[2]))
local global_count = redis.call('ZCARD', KEYS[1])
local has_principal = tonumber(ARGV[6]) == 1
local principal_count = 0
if has_principal then
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[1] - tonumber(ARGV[2]))
    principal_count = redis.call('ZCARD', KEYS[2])
end
local max_global = tonumber(ARGV[3])
local max_principal = tonumber(ARGV[4])
if max_global >= 0 and global_count >= max_global then
    return {0, global_count}
end
if has_principal and max_principal >= 0 and principal_count >= max_principal then
    return {0, global_count}
end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[5])
redis.call('PEXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2]) * 2000))
if has_principal then
    redis.call('ZADD', KEYS[2], ARGV[1], ARGV[5])
    redis.call('PEXPIRE', KEYS[2], math.ceil(tonumber(ARGV[2]) * 2000))
end
return {1, global_count + 1}
"""

# KEYS[1] = global per-connection queue key
# KEYS[2] = per-connection-per-principal queue key (unused if ARGV[2] is '0')
# ARGV[1] = this waiter's token
# ARGV[2] = '1' if a principal-scoped cap applies, else '0'
_QUEUE_LEAVE_SCRIPT = """
redis.call('ZREM', KEYS[1], ARGV[1])
if tonumber(ARGV[2]) == 1 then
    redis.call('ZREM', KEYS[2], ARGV[1])
end
return redis.call('ZCARD', KEYS[1])
"""

# A negative depth is the "unknown, Redis was unreachable" sentinel — callers
# must not treat -1 as an actual queue length (see enter_queue/leave_queue).
_QUEUE_DEPTH_UNKNOWN = -1


class RedisConcurrencyLimiter:
    def __init__(
        self,
        redis_client: "Redis",
        *,
        lease_seconds: float = 120,
        poll_interval_seconds: float = 0.05,
        fail_open: bool = True,
    ) -> None:
        self._redis = redis_client
        self._lease_seconds = lease_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._fail_open = fail_open
        self._script = redis_client.register_script(_ACQUIRE_SCRIPT)
        self._queue_enter_script = redis_client.register_script(_QUEUE_ENTER_SCRIPT)
        self._queue_leave_script = redis_client.register_script(_QUEUE_LEAVE_SCRIPT)

    async def acquire(self, connection_id: str, max_concurrency: int, wait_seconds: float) -> str:
        key = _key(connection_id)
        token = uuid.uuid4().hex
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                acquired = await self._script(
                    keys=[key], args=[time.time(), self._lease_seconds, max_concurrency, token]
                )
            except RedisError as exc:
                if not self._fail_open:
                    raise
                # Prioritizes availability over strict enforcement: a Redis
                # outage degrades to "concurrency unlimited" rather than
                # rejecting every query. Set concurrency_redis_fail_open=false
                # for deployments that would rather fail closed.
                get_logger().warning(
                    "concurrency.redis.unreachable_fail_open",
                    connection=connection_id,
                    error=str(exc),
                )
                return _FAIL_OPEN_TOKEN
            if acquired:
                return token
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"too many concurrent {connection_id!r} queries in flight, try again shortly"
                )
            await asyncio.sleep(self._poll_interval_seconds)

    async def release(self, connection_id: str, token: str) -> None:
        if token == _FAIL_OPEN_TOKEN:
            return
        try:
            await self._redis.zrem(_key(connection_id), token)
        except RedisError as exc:
            # The lease TTL reclaims this slot on its own even if the
            # explicit release fails, so this is a delayed-recovery
            # warning, not a correctness problem.
            get_logger().warning(
                "concurrency.redis.release_failed", connection=connection_id, error=str(exc)
            )

    async def enter_queue(
        self,
        connection_id: str,
        *,
        principal_subject: Optional[str],
        max_queue_depth: Optional[int],
        max_queue_depth_per_principal: Optional[int],
    ) -> Optional[tuple[str, int]]:
        """Admit one waiter into cross-replica queue-depth tracking.

        Returns `(token, global_depth_after_admission)` on success — the
        depth is the true cross-replica count, for `querygate_queue_depth`.
        Returns `None` if either cap is already met (the caller must reject
        with `QueueDepthExceededError`; this module deliberately raises no
        QueryGate-specific exception, matching `acquire()`'s plain
        `TimeoutError`). Fails open on a Redis error the same way `acquire()`
        does — depth is reported as `_QUEUE_DEPTH_UNKNOWN` so the caller knows
        not to trust it.
        """
        key = _queue_key(connection_id)
        has_principal = principal_subject is not None
        principal_key = (
            _queue_principal_key(connection_id, principal_subject) if has_principal else key
        )
        token = uuid.uuid4().hex
        try:
            allowed, depth = await self._queue_enter_script(
                keys=[key, principal_key],
                args=[
                    time.time(),
                    self._lease_seconds,
                    max_queue_depth if max_queue_depth is not None else -1,
                    (
                        max_queue_depth_per_principal
                        if max_queue_depth_per_principal is not None
                        else -1
                    ),
                    token,
                    1 if has_principal else 0,
                ],
            )
        except RedisError as exc:
            if not self._fail_open:
                raise
            get_logger().warning(
                "concurrency.redis.queue_unreachable_fail_open",
                connection=connection_id,
                error=str(exc),
            )
            return _FAIL_OPEN_TOKEN, _QUEUE_DEPTH_UNKNOWN
        if not allowed:
            return None
        return token, int(depth)

    async def leave_queue(
        self, connection_id: str, *, principal_subject: Optional[str], token: str
    ) -> int:
        """Release a waiter admitted by `enter_queue`; returns the true
        cross-replica depth after removal, or `_QUEUE_DEPTH_UNKNOWN` if that
        can't be determined (fail-open token, or a Redis error on the way
        out — the lease TTL reclaims the entry either way, so this is a
        delayed-recovery warning, not a correctness problem).
        """
        if token == _FAIL_OPEN_TOKEN:
            return _QUEUE_DEPTH_UNKNOWN
        key = _queue_key(connection_id)
        has_principal = principal_subject is not None
        principal_key = (
            _queue_principal_key(connection_id, principal_subject) if has_principal else key
        )
        try:
            depth = await self._queue_leave_script(
                keys=[key, principal_key], args=[token, 1 if has_principal else 0]
            )
        except RedisError as exc:
            get_logger().warning(
                "concurrency.redis.queue_release_failed", connection=connection_id, error=str(exc)
            )
            return _QUEUE_DEPTH_UNKNOWN
        return int(depth)


def _key(connection_id: str) -> str:
    return f"querygate:concurrency:{connection_id}"


def _queue_key(connection_id: str) -> str:
    return f"querygate:queue:{connection_id}"


def _queue_principal_key(connection_id: str, principal_subject: str) -> str:
    return f"querygate:queue:{connection_id}:{principal_subject}"
