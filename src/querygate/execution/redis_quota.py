"""Redis-backed cross-replica per-principal query quota (TODO.md item 50 phase 2).

The distributed sibling of `quota.InProcessQuotaLimiter`: it makes a principal's
rolling-window request/byte budget a single **shared** budget across every
replica, instead of one that silently multiplies by replica count (the exact
gap `deploy/HA_DR.md`'s shared-state matrix flags for the in-process limiter).

Design mirrors `redis_concurrency.py`: each key is a per-(connection, principal)
sorted set scored by wall-clock time; a single Lua script atomically prunes aged
entries, checks the request-count and byte-total caps against the true
cross-replica window, and — only if admitted — records the attempt. Response
bytes (unknown until the query runs) live in a parallel hash keyed by the same
member id, filled in by `record_bytes` after execution; the prune removes a
member from both structures together so a total is never stale. Both keys carry
a window-length TTL so an idle principal's window disappears on its own.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from redis.exceptions import RedisError

from querygate.core.exceptions import QuotaExceededError
from querygate.core.logging import get_logger
from querygate.execution.quota import QuotaKey, QuotaReservation

# KEYS[1]=zset (member->ts), KEYS[2]=hash (member->bytes).
# ARGV: now, window, max_requests (-1 = unlimited), max_bytes (-1 = unlimited), new_member.
# Returns {0} on admit; {1, retry_after} on request cap; {2, retry_after} on byte cap.
_RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cutoff = now - window
local aged = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', cutoff)
for _, m in ipairs(aged) do redis.call('HDEL', KEYS[2], m) end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)

local function retry_after()
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local oldest_ts = oldest[2] and tonumber(oldest[2]) or now
  local r = math.ceil(window - (now - oldest_ts))
  if r < 1 then r = 1 end
  return r
end

local max_requests = tonumber(ARGV[3])
if max_requests >= 0 and redis.call('ZCARD', KEYS[1]) >= max_requests then
  return {1, retry_after()}
end
local max_bytes = tonumber(ARGV[4])
if max_bytes >= 0 then
  local total = 0
  for _, v in ipairs(redis.call('HVALS', KEYS[2])) do total = total + tonumber(v) end
  if total >= max_bytes then return {2, retry_after()} end
end

redis.call('ZADD', KEYS[1], now, ARGV[5])
redis.call('HSET', KEYS[2], ARGV[5], 0)
local ttl = math.ceil(window)
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
return {0}
"""

# Only credit bytes if the member is still in the window (not aged out), so a
# late record can't resurrect a pruned entry into the hash.
_RECORD_BYTES_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
end
return 1
"""


def _keys(key: QuotaKey) -> tuple[str, str]:
    connection_id, principal = key
    base = f"qg:quota:{connection_id}:{principal}"
    return base, f"{base}:bytes"


class RedisQuotaLimiter:
    """Cross-replica quota limiter satisfying the async `QuotaLimiter` shape."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client
        self._reserve = redis_client.register_script(_RESERVE_SCRIPT)
        self._record = redis_client.register_script(_RECORD_BYTES_SCRIPT)

    async def reserve(
        self,
        key: QuotaKey,
        *,
        max_requests: Optional[int],
        max_response_bytes: Optional[int],
        window_seconds: int,
        now: Optional[float] = None,
    ) -> QuotaReservation:
        zkey, hkey = _keys(key)
        member = uuid.uuid4().hex
        try:
            result = await self._reserve(
                keys=[zkey, hkey],
                args=[
                    time.time() if now is None else now,
                    window_seconds,
                    -1 if max_requests is None else max_requests,
                    -1 if max_response_bytes is None else max_response_bytes,
                    member,
                ],
            )
        except RedisError as exc:
            # Prioritize availability over strict enforcement, matching the
            # concurrency limiter's default posture: a Redis outage degrades to
            # "quota unenforced" (admit) rather than rejecting every query. The
            # returned reservation carries no Redis state, so `record_bytes` is
            # a harmless no-op.
            get_logger().warning("quota.redis.unreachable_fail_open", error=str(exc))
            return QuotaReservation()
        code = int(result[0])
        if code == 0:
            return QuotaReservation(redis_key=hkey, redis_member=member)
        retry_after = int(result[1])
        if code == 1:
            raise QuotaExceededError(
                f"request quota exceeded: at most {max_requests} queries per "
                f"{window_seconds}s window; retry in ~{retry_after}s",
                quota_kind="requests",
                retry_after_seconds=retry_after,
            )
        raise QuotaExceededError(
            f"response-byte quota exceeded: at most {max_response_bytes} bytes per "
            f"{window_seconds}s window; retry in ~{retry_after}s",
            quota_kind="bytes",
            retry_after_seconds=retry_after,
        )

    async def record_bytes(self, reservation: QuotaReservation, response_bytes: int) -> None:
        if reservation._redis_key is None or reservation._redis_member is None:
            return
        # The member lives in the zset (KEYS[1]); its bytes go in the hash
        # (KEYS[2] == reservation._redis_key). Derive the zset key from the hash.
        zkey = reservation._redis_key[: -len(":bytes")]
        try:
            await self._record(
                keys=[zkey, reservation._redis_key],
                args=[reservation._redis_member, max(0, response_bytes)],
            )
        except RedisError as exc:
            # The query already succeeded; a failed byte attribution must not
            # turn into a caller-visible error. The member's TTL still ages the
            # (bytes-0) entry out of the window on its own.
            get_logger().warning("quota.redis.record_bytes_failed", error=str(exc))
