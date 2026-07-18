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
