"""Redis-backed cross-replica cumulative disclosure budget (TODO.md item 179).

The distributed sibling of `disclosure_budget.InProcessDisclosureBudgetLimiter`:
it makes a principal's per-table probe budget a single **shared** budget across
every replica, instead of one that silently multiplies by replica count. Without
it, an N-replica deployment gives a prober N× the probes the operator
configured — which for a privacy control is worse than for a rate quota, since
the operator believes a specific k-anonymity guarantee is being defended.

Design mirrors `redis_quota.py`: each key is a sorted set scored by wall-clock
time, and one Lua script does everything atomically. The difference is that a
single query charges SEVERAL keys at once (a per-table counter and a per-shape
counter, for each table it aggregates over), and the charge must be
all-or-nothing — so the script checks every cap across every key first and only
then records any of them. A query refused on its third key has spent nothing on
its first two. Every key carries a window-length TTL, so an idle principal's
window disappears on its own.

**Fail-closed, deliberately diverging from `redis_quota.py`/`redis_concurrency.py`.**
Those two fail *open* on a Redis outage: they are anti-abuse/capacity controls,
and admitting traffic is the right degradation. This budget is a privacy
control defending `min_group_size`'s k-anonymity guarantee, and failing open
would silently suspend that defense exactly when nobody is watching, while the
operator's configuration still says it is enforced. So an unreachable Redis
refuses the aggregate query instead. The blast radius is bounded by design: only
aggregate queries on connections that set BOTH `min_group_size` and a disclosure
cap are affected — every other read is untouched — and an operator who prefers
availability can unset the caps. A configurable posture (mirroring
`concurrency_redis_fail_open`) is deliberately NOT added here; if real operations
show it is needed it should be its own reviewed change, not a default softened
in passing.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional, Sequence
from urllib.parse import quote

from redis.exceptions import RedisError

from querygate.core.exceptions import DisclosureBudgetExceededError
from querygate.core.logging import get_logger
from querygate.execution.disclosure_budget import (
    Charge,
    DisclosureBudgetKey,
    rejection_message,
)

# KEYS = one zset per charge. ARGV[1]=now, ARGV[2]=window, ARGV[3]=member prefix,
# then, in the same order as KEYS, ARGV[3+i] = that key's limit and
# ARGV[3+#KEYS+i] = that key's weight (how many units this query costs it).
# Returns {0} on admit; {i, retry_after} when KEYS[i] cannot take its weight.
_RESERVE_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
local cutoff = now - window
local n = #KEYS

for i = 1, n do
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', cutoff)
  local limit = tonumber(ARGV[3 + i])
  local weight = tonumber(ARGV[3 + n + i])
  if redis.call('ZCARD', KEYS[i]) + weight > limit then
    -- Read the oldest member first, then its score with ZSCORE, rather than
    -- ZRANGE ... WITHSCORES: the Lua bridge surfaces a WITHSCORES reply as a
    -- NESTED table, so `reply[2]` is nil and the countdown silently collapses
    -- to the full window on every rejection. Measured under fakeredis; the
    -- two-call form is correct on both. (`redis_quota.py` still uses the
    -- WITHSCORES form — see this item's write-up.)
    local oldest = redis.call('ZRANGE', KEYS[i], 0, 0)
    local oldest_ts = now
    if oldest[1] then
      local score = redis.call('ZSCORE', KEYS[i], oldest[1])
      if score then oldest_ts = tonumber(score) end
    end
    local r = math.ceil(window - (now - oldest_ts))
    if r < 1 then r = 1 end
    return {i, r}
  end
end

local ttl = math.ceil(window)
for i = 1, n do
  local weight = tonumber(ARGV[3 + n + i])
  for w = 1, weight do
    redis.call('ZADD', KEYS[i], now, member .. ':' .. i .. ':' .. w)
  end
  redis.call('EXPIRE', KEYS[i], ttl)
end
return {0}
"""


def _redis_key(key: DisclosureBudgetKey) -> str:
    """Flatten a budget key into one Redis key.

    Every component is percent-escaped before joining. Two of them —
    `principal` (an IdP-issued subject) and `purpose` (free text up to 200
    chars when the connection has not opted into purpose gating) — can legally
    contain the `:` separator, so a raw f-string would let two distinct logical
    keys flatten to the same Redis key. The in-process backend keys on a tuple
    and has no such ambiguity; escaping is what keeps the two implementations of
    this Protocol agreeing on key identity.
    """
    return "qg:disclosure:" + ":".join(quote(part, safe="") for part in key)


class RedisDisclosureBudgetLimiter:
    """Cross-replica limiter satisfying the async `DisclosureBudgetLimiter`
    shape."""

    def __init__(self, redis_client) -> None:
        self._redis = redis_client
        self._reserve = redis_client.register_script(_RESERVE_SCRIPT)

    async def reserve(
        self,
        charges: Sequence[Charge],
        *,
        window_seconds: int,
        now: Optional[float] = None,
    ) -> None:
        if not charges:
            return
        member = uuid.uuid4().hex
        keys = [_redis_key(key) for key, _limit, _kind, _weight in charges]
        limits = [limit for _key, limit, _kind, _weight in charges]
        weights = [weight for _key, _limit, _kind, weight in charges]
        try:
            result = await self._reserve(
                keys=keys,
                args=[
                    time.time() if now is None else now,
                    window_seconds,
                    member,
                    *limits,
                    *weights,
                ],
            )
        except RedisError as exc:
            # Fail CLOSED — see the module docstring for why this diverges from
            # the quota/concurrency limiters. The error text says the guardrail
            # could not be evaluated, never that a budget was exhausted, so an
            # operator reading logs isn't misled into tuning a threshold that
            # was never consulted.
            get_logger().error("disclosure_budget.redis.unreachable_fail_closed", error=str(exc))
            raise DisclosureBudgetExceededError(
                "disclosure budget could not be evaluated, so this aggregate query "
                "was refused; retry shortly",
                quota_kind=charges[0][2],
                retry_after_seconds=1,
            ) from exc
        index = int(result[0])
        if index == 0:
            return
        retry_after = int(result[1])
        _key, limit, kind, _weight = charges[index - 1]
        raise DisclosureBudgetExceededError(
            rejection_message(kind, limit, window_seconds, retry_after),
            quota_kind=kind,
            retry_after_seconds=retry_after,
        )
