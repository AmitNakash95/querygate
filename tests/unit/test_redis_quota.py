"""RedisQuotaLimiter against fakeredis (TODO.md item 50 phase 2).

Proves the cross-replica quota's window semantics — request-count and byte
caps, rolling expiry, per-(connection, principal) isolation, and record_bytes
attribution — hold via the Lua script, and (standing in for cross-replica) that
two separate limiter instances sharing one Redis enforce a single shared budget.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from redis.exceptions import RedisError

from querygate.core.exceptions import QuotaExceededError
from querygate.execution.redis_quota import RedisQuotaLimiter

pytestmark = pytest.mark.unit


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


@pytest.mark.asyncio
async def test_request_cap_admits_then_rejects_with_retry_after(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    await lim.reserve(key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0)
    await lim.reserve(key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0)
    with pytest.raises(QuotaExceededError) as exc:
        await lim.reserve(
            key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0
        )
    assert exc.value.quota_kind == "requests"
    assert exc.value.retry_after_seconds == 30


@pytest.mark.asyncio
async def test_window_rolls_forward(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    await lim.reserve(key, max_requests=1, max_response_bytes=None, window_seconds=30, now=100.0)
    with pytest.raises(QuotaExceededError):
        await lim.reserve(
            key, max_requests=1, max_response_bytes=None, window_seconds=30, now=110.0
        )
    # Once the first attempt ages past the window, capacity frees up again.
    await lim.reserve(key, max_requests=1, max_response_bytes=None, window_seconds=30, now=131.0)


@pytest.mark.asyncio
async def test_byte_cap_and_record_bytes(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    r1 = await lim.reserve(
        key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=100.0
    )
    await lim.record_bytes(r1, 600)
    r2 = await lim.reserve(
        key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=100.0
    )
    await lim.record_bytes(r2, 600)  # window now holds 1200 bytes
    with pytest.raises(QuotaExceededError) as exc:
        await lim.reserve(
            key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=100.0
        )
    assert exc.value.quota_kind == "bytes"


@pytest.mark.asyncio
async def test_principals_and_connections_are_isolated(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    await lim.reserve(
        ("demo", "a"), max_requests=1, max_response_bytes=None, window_seconds=30, now=100.0
    )
    # Same connection, different principal, and same principal different
    # connection each get their own budget.
    await lim.reserve(
        ("demo", "b"), max_requests=1, max_response_bytes=None, window_seconds=30, now=100.0
    )
    await lim.reserve(
        ("other", "a"), max_requests=1, max_response_bytes=None, window_seconds=30, now=100.0
    )
    with pytest.raises(QuotaExceededError):
        await lim.reserve(
            ("demo", "a"), max_requests=1, max_response_bytes=None, window_seconds=30, now=100.0
        )


@pytest.mark.asyncio
async def test_two_limiter_instances_share_one_budget(redis_client):
    # Stands in for two replicas: separate limiter objects, one Redis — the
    # budget is shared, unlike the per-replica in-process limiter.
    a = RedisQuotaLimiter(redis_client)
    b = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    await a.reserve(key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0)
    await b.reserve(key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0)
    with pytest.raises(QuotaExceededError):
        await a.reserve(key, max_requests=2, max_response_bytes=None, window_seconds=30, now=100.0)


@pytest.mark.asyncio
async def test_record_bytes_on_aged_out_reservation_is_harmless(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    r1 = await lim.reserve(
        key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=100.0
    )
    # Advance well past the window so r1 is pruned on the next reserve.
    await lim.reserve(key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=200.0)
    await lim.record_bytes(r1, 5000)  # must not resurrect the pruned entry's bytes
    # Still admitted (the 5000 didn't count, since r1 aged out).
    await lim.reserve(key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=200.0)


@pytest.mark.asyncio
async def test_reserve_fails_open_on_redis_error(redis_client):
    # A Redis outage degrades to "admit" rather than rejecting every query,
    # matching the concurrency limiter's default posture.
    lim = RedisQuotaLimiter(redis_client)

    async def _boom(*args, **kwargs):
        raise RedisError("unreachable")

    lim._reserve = _boom
    key = ("demo", "agent")
    reservation = await lim.reserve(
        key, max_requests=0, max_response_bytes=None, window_seconds=30, now=100.0
    )
    # Reservation carries no Redis state, so record_bytes is a harmless no-op.
    assert reservation._redis_key is None
    await lim.record_bytes(reservation, 500)


@pytest.mark.asyncio
async def test_record_bytes_swallows_redis_error(redis_client):
    lim = RedisQuotaLimiter(redis_client)
    key = ("demo", "agent")
    r1 = await lim.reserve(
        key, max_requests=None, max_response_bytes=1000, window_seconds=30, now=100.0
    )

    async def _boom(*args, **kwargs):
        raise RedisError("unreachable")

    lim._record = _boom
    # The query already completed; a failed byte attribution must not raise.
    await lim.record_bytes(r1, 600)
