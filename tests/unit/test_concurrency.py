"""Unit tests for the per-connection concurrency guardrail."""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from querygate.execution import concurrency as cc
from querygate.execution.redis_concurrency import RedisConcurrencyLimiter
from querygate.metrics import REGISTRY


def _gauge(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_updates_in_use_gauge():
    cc.SEMAPHORES.pop("demo", None)
    assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 0.0

    async with cc.concurrency_slot("demo", max_concurrency=2, wait_seconds=1):
        assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 1.0

    assert _gauge("querygate_concurrency_in_use", {"connection": "demo"}) == 0.0
    assert _gauge("querygate_concurrency_max", {"connection": "demo"}) == 2.0


@pytest.mark.asyncio
async def test_concurrency_cap_serializes_in_flight_calls():
    cc.SEMAPHORES.pop("demo", None)
    in_flight = 0
    max_in_flight = 0

    async def work():
        nonlocal in_flight, max_in_flight
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1

    await asyncio.gather(work(), work(), work())
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_concurrency_cap_raises_when_wait_exceeded():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

    with pytest.raises(ValueError, match="too many concurrent"):
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=0.05):
            pass  # pragma: no cover


@pytest.mark.asyncio
async def test_concurrency_caps_are_independent_per_connection():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()
    cc.SEMAPHORES.pop("other", None)

    # Must not block on "demo"'s exhausted slot.
    async with cc.concurrency_slot("other", max_concurrency=1, wait_seconds=1):
        pass


@pytest.mark.asyncio
async def test_concurrency_slot_tracks_queue_depth_while_waiting():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0

    async def _waiter():
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            pass

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.02)  # let the waiter actually start blocking on acquire
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 1.0

    cc.SEMAPHORES["demo"].release()  # frees the slot the waiter is queued behind
    await task
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_releases_queue_depth_after_a_capacity_timeout():
    cc.SEMAPHORES["demo"] = asyncio.Semaphore(1)
    await cc.SEMAPHORES["demo"].acquire()  # occupy the only slot

    with pytest.raises(ValueError, match="too many concurrent"):
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=0.05):
            pass  # pragma: no cover
    # Depth must be released even though the acquire ultimately timed out.
    assert _gauge("querygate_queue_depth", {"connection": "demo"}) == 0.0


@pytest.mark.asyncio
async def test_concurrency_slot_dispatches_to_redis_limiter_when_configured():
    limiter = RedisConcurrencyLimiter(fakeredis.aioredis.FakeRedis(), lease_seconds=30)
    cc.init_redis_limiter(limiter)
    try:
        # SEMAPHORES must stay untouched — proves the in-process path was
        # never exercised for this call.
        cc.SEMAPHORES.pop("demo", None)
        async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
            assert "demo" not in cc.SEMAPHORES
    finally:
        cc.clear_redis_limiter()


@pytest.mark.asyncio
async def test_clear_redis_limiter_reverts_to_in_process():
    limiter = RedisConcurrencyLimiter(fakeredis.aioredis.FakeRedis(), lease_seconds=30)
    cc.init_redis_limiter(limiter)
    cc.clear_redis_limiter()

    cc.SEMAPHORES.pop("demo", None)
    async with cc.concurrency_slot("demo", max_concurrency=1, wait_seconds=1):
        assert "demo" in cc.SEMAPHORES
