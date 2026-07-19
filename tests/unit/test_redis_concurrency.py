"""Unit tests for RedisConcurrencyLimiter — a real Lua-scripted sorted-set
semaphore run against fakeredis (in-memory, no real Redis server), so the
actual acquire/release/lease-expiry logic is exercised, not just mocked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from querygate.execution.redis_concurrency import RedisConcurrencyLimiter


@pytest.fixture
def redis_client():
    return fakeredis.aioredis.FakeRedis()


@pytest.mark.asyncio
async def test_acquire_and_release_within_capacity(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    token = await limiter.acquire("demo", max_concurrency=2, wait_seconds=1)
    assert token
    await limiter.release("demo", token)


@pytest.mark.asyncio
async def test_enforces_capacity_across_separate_limiter_instances(redis_client):
    """Two RedisConcurrencyLimiter objects sharing one redis_client stand in
    for two separate QueryGate instances sharing one Redis — the whole
    point of this feature.
    """
    limiter_a = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    limiter_b = RedisConcurrencyLimiter(redis_client, lease_seconds=30)

    token_a = await limiter_a.acquire("demo", max_concurrency=1, wait_seconds=1)
    assert token_a

    with pytest.raises(TimeoutError, match="too many concurrent"):
        await limiter_b.acquire("demo", max_concurrency=1, wait_seconds=0.2)

    await limiter_a.release("demo", token_a)
    token_b = await limiter_b.acquire("demo", max_concurrency=1, wait_seconds=1)
    assert token_b


@pytest.mark.asyncio
async def test_connections_are_independent(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    token = await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)
    assert token

    # Must not block on "demo"'s exhausted slot.
    other_token = await limiter.acquire("other", max_concurrency=1, wait_seconds=1)
    assert other_token


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=0.05, poll_interval_seconds=0.02)
    # Simulates a crashed holder: acquired but never released.
    await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)
    await asyncio.sleep(0.1)  # outlive the lease

    # A fresh acquire should reclaim the expired slot rather than time out.
    token = await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)
    assert token


@pytest.mark.asyncio
async def test_release_is_idempotent_and_only_frees_its_own_slot(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    token_1 = await limiter.acquire("demo", max_concurrency=2, wait_seconds=1)
    token_2 = await limiter.acquire("demo", max_concurrency=2, wait_seconds=1)

    await limiter.release("demo", token_1)
    # Capacity 2, one released -> exactly one free slot, not two.
    token_3 = await limiter.acquire("demo", max_concurrency=2, wait_seconds=0.2)
    assert token_3
    with pytest.raises(TimeoutError):
        await limiter.acquire("demo", max_concurrency=2, wait_seconds=0.2)

    await limiter.release("demo", token_2)
    await limiter.release("demo", token_3)


class _BrokenScript:
    async def __call__(self, keys, args):
        raise RedisConnectionError("connection refused")


class _BrokenRedisClient:
    def register_script(self, script_body):
        return _BrokenScript()

    async def zrem(self, key, token):
        raise RedisConnectionError("connection refused")


@pytest.mark.asyncio
async def test_fail_open_returns_sentinel_when_redis_unreachable():
    limiter = RedisConcurrencyLimiter(_BrokenRedisClient(), fail_open=True)
    token = await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)
    assert token
    await limiter.release("demo", token)  # must not raise — skips contacting redis again


@pytest.mark.asyncio
async def test_fail_closed_raises_when_redis_unreachable():
    limiter = RedisConcurrencyLimiter(_BrokenRedisClient(), fail_open=False)
    with pytest.raises(RedisConnectionError):
        await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)


@pytest.mark.asyncio
async def test_release_failure_is_swallowed_not_raised(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    token = await limiter.acquire("demo", max_concurrency=1, wait_seconds=1)
    with patch.object(redis_client, "zrem", AsyncMock(side_effect=RedisConnectionError("down"))):
        await limiter.release("demo", token)  # must not raise


# --- Queue-depth tracking (TODO.md item 35 phase 2) -------------------------


@pytest.mark.asyncio
async def test_enter_queue_and_leave_queue_within_capacity(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    entered = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=2, max_queue_depth_per_principal=None
    )
    assert entered is not None
    token, depth = entered
    assert depth == 1
    depth_after = await limiter.leave_queue("demo", principal_subject=None, token=token)
    assert depth_after == 0


@pytest.mark.asyncio
async def test_enter_queue_rejects_once_max_queue_depth_is_met(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    first = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert first is not None

    second = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert second is None  # queue already at its cap of 1


@pytest.mark.asyncio
async def test_enter_queue_enforces_cap_across_separate_limiter_instances(redis_client):
    limiter_a = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    limiter_b = RedisConcurrencyLimiter(redis_client, lease_seconds=30)

    first = await limiter_a.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert first is not None

    second = await limiter_b.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert second is None


@pytest.mark.asyncio
async def test_enter_queue_none_caps_are_unlimited(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    for _ in range(5):
        entered = await limiter.enter_queue(
            "demo",
            principal_subject=None,
            max_queue_depth=None,
            max_queue_depth_per_principal=None,
        )
        assert entered is not None


@pytest.mark.asyncio
async def test_max_queue_depth_per_principal_is_isolated_from_other_principals(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    first = await limiter.enter_queue(
        "demo",
        principal_subject="noisy-agent",
        max_queue_depth=None,
        max_queue_depth_per_principal=1,
    )
    assert first is not None

    same_principal_again = await limiter.enter_queue(
        "demo",
        principal_subject="noisy-agent",
        max_queue_depth=None,
        max_queue_depth_per_principal=1,
    )
    assert same_principal_again is None  # noisy-agent already has 1 queued

    other_principal = await limiter.enter_queue(
        "demo",
        principal_subject="other-agent",
        max_queue_depth=None,
        max_queue_depth_per_principal=1,
    )
    assert other_principal is not None  # a different principal is unaffected


@pytest.mark.asyncio
async def test_leave_queue_frees_capacity_for_a_new_waiter(redis_client):
    limiter = RedisConcurrencyLimiter(redis_client, lease_seconds=30)
    entered = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert entered is not None
    token, _ = entered

    await limiter.leave_queue("demo", principal_subject=None, token=token)

    reentered = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert reentered is not None


@pytest.mark.asyncio
async def test_enter_queue_fail_open_returns_sentinel_when_redis_unreachable():
    limiter = RedisConcurrencyLimiter(_BrokenRedisClient(), fail_open=True)
    entered = await limiter.enter_queue(
        "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
    )
    assert entered is not None
    token, depth = entered
    assert depth == -1  # unknown — Redis was unreachable, don't trust this as a real count
    await limiter.leave_queue("demo", principal_subject=None, token=token)  # must not raise


@pytest.mark.asyncio
async def test_enter_queue_fail_closed_raises_when_redis_unreachable():
    limiter = RedisConcurrencyLimiter(_BrokenRedisClient(), fail_open=False)
    with pytest.raises(RedisConnectionError):
        await limiter.enter_queue(
            "demo", principal_subject=None, max_queue_depth=1, max_queue_depth_per_principal=None
        )
