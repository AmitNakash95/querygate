"""Redis-backed distributed concurrency limiter.

`execution/concurrency.py`'s default `SEMAPHORES` dict is an in-process
`asyncio.Semaphore` per connection id — run N instances behind a load
balancer with `max_concurrency: 8` and the real ceiling against that
database is `8 * N`, not 8, silently defeating the configured guardrail at
exactly the moment it matters (a traffic burst that triggers autoscaling).
`RedisConcurrencyLimiter` enforces one shared budget per connection id
across every instance that points at the same Redis.

Design: each held slot is a member of a per-connection Redis sorted set,
scored by acquisition time. Acquiring is a single Lua script (atomic —
avoids a check-then-set race between instances) that first drops members
older than `lease_seconds` (reclaiming a slot from an instance that crashed
mid-query without releasing it) and then adds the new member only if the
set is under capacity. There's no native blocking primitive for a sorted
set, so waiting for a free slot is short-interval polling, not a blocking
call — bounded by `wait_seconds`, matching the in-process semaphore's
`asyncio.wait_for` timeout semantics.
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


def _key(connection_id: str) -> str:
    return f"querygate:concurrency:{connection_id}"
